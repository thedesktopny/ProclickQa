from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import sqlite3
import hashlib
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='.')
app.secret_key = 'voiceguard-secret-2024-change-this'
CORS(app)

DB_PATH = 'rules.db'

# ─── ADMIN CREDENTIALS (change these) ───────────────────────────────────────
ADMIN_USERNAME = 'david'
ADMIN_PASSWORD = hashlib.sha256('admin123'.encode()).hexdigest()  # change 'admin123'

# ─── DATABASE SETUP ──────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            extension TEXT NOT NULL,
            email TEXT,
            photo TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT,
            agent_name TEXT,
            duration TEXT,
            overall_score INTEGER,
            emotion TEXT,
            status TEXT,
            flags INTEGER DEFAULT 0,
            scorecard TEXT,
            transcript TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Insert default rules if table is empty
    c.execute('SELECT COUNT(*) FROM rules')
    if c.fetchone()[0] == 0:
        default_rules = [
            ('Agent must never use inappropriate, offensive, or sexual language of any kind', 'Forbidden Words', 'Critical'),
            ('Agent must read the total price including shipping out loud before placing any order', 'Compliance', 'Critical'),
            ('Agent must receive explicit verbal confirmation from customer before completing any purchase', 'Compliance', 'Critical'),
            ('Agent should not overuse the word "sir" — using it too frequently sounds robotic', 'Behavior', 'Warning'),
            ('If a customer sounds frustrated or upset, agent must acknowledge their feelings before continuing', 'Behavior', 'Warning'),
            ('Agent must verify the customer identity at the start of every call before accessing any account', 'Compliance', 'Warning'),
            ('Agent must verbally confirm "I have logged out of your account" before ending any call where account access occurred', 'Compliance', 'Info'),
            ('Agent must ask "Is there anything else I can help you with today?" before ending the call', 'Required Phrases', 'Info'),
            ('If a customer mentions a competitor, agent must not speak negatively about them', 'Behavior', 'Info'),
        ]
        c.executemany('INSERT INTO rules (description, category, severity) VALUES (?,?,?)', default_rules)

    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '')
    password = hashlib.sha256(data.get('password', '').encode()).hexdigest()
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/auth-check', methods=['GET'])
def auth_check():
    return jsonify({'authenticated': session.get('admin', False)})

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# ─── RULES API ────────────────────────────────────────────────────────────────
@app.route('/api/rules', methods=['GET'])
def get_rules():
    conn = get_db()
    rules = conn.execute('SELECT * FROM rules ORDER BY severity DESC, id ASC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rules])

@app.route('/api/rules', methods=['POST'])
@require_admin
def add_rule():
    data = request.json
    description = data.get('description', '').strip()
    category = data.get('category', 'Behavior')
    severity = data.get('severity', 'Warning')

    if not description:
        return jsonify({'error': 'Description is required'}), 400

    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO rules (description, category, severity) VALUES (?,?,?)',
        (description, category, severity)
    )
    rule_id = cursor.lastrowid
    conn.commit()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    conn.close()
    return jsonify(dict(rule)), 201

@app.route('/api/rules/<int:rule_id>', methods=['PUT'])
@require_admin
def update_rule(rule_id):
    data = request.json
    conn = get_db()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404

    description = data.get('description', rule['description'])
    category = data.get('category', rule['category'])
    severity = data.get('severity', rule['severity'])
    active = data.get('active', rule['active'])

    conn.execute(
        'UPDATE rules SET description=?, category=?, severity=?, active=? WHERE id=?',
        (description, category, severity, active, rule_id)
    )
    conn.commit()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    conn.close()
    return jsonify(dict(rule))

@app.route('/api/rules/<int:rule_id>', methods=['DELETE'])
@require_admin
def delete_rule(rule_id):
    conn = get_db()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404
    conn.execute('DELETE FROM rules WHERE id=?', (rule_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/rules/<int:rule_id>/toggle', methods=['POST'])
@require_admin
def toggle_rule(rule_id):
    conn = get_db()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404
    new_active = 0 if rule['active'] else 1
    conn.execute('UPDATE rules SET active=? WHERE id=?', (new_active, rule_id))
    conn.commit()
    rule = conn.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
    conn.close()
    return jsonify(dict(rule))

# ─── CALLS API ────────────────────────────────────────────────────────────────
@app.route('/api/calls', methods=['GET'])
def get_calls():
    conn = get_db()
    calls = conn.execute('SELECT * FROM calls ORDER BY created_at DESC LIMIT 50').fetchall()
    conn.close()
    return jsonify([dict(c) for c in calls])

@app.route('/api/calls', methods=['POST'])
def add_call():
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        '''INSERT INTO calls (call_id, agent_name, duration, overall_score, emotion, status, flags, scorecard, transcript)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            data.get('call_id'),
            data.get('agent_name'),
            data.get('duration'),
            data.get('overall_score'),
            data.get('emotion'),
            data.get('status'),
            data.get('flags', 0),
            str(data.get('scorecard', '')),
            data.get('transcript', '')
        )
    )
    call_id = cursor.lastrowid
    conn.commit()
    call = conn.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
    conn.close()
    return jsonify(dict(call)), 201

# ─── STATS API ────────────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_db()
    total_calls = conn.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
    avg_score = conn.execute('SELECT AVG(overall_score) FROM calls').fetchone()[0]
    critical_flags = conn.execute("SELECT COUNT(*) FROM calls WHERE status='Critical'").fetchone()[0]
    needs_coaching = conn.execute('SELECT COUNT(DISTINCT agent_name) FROM calls WHERE overall_score < 70').fetchone()[0]
    active_rules = conn.execute('SELECT COUNT(*) FROM rules WHERE active=1').fetchone()[0]
    conn.close()

    return jsonify({
        'total_calls': total_calls,
        'avg_score': round(avg_score or 0, 1),
        'critical_flags': critical_flags,
        'needs_coaching': needs_coaching,
        'active_rules': active_rules
    })

# ─── AGENTS API ───────────────────────────────────────────────────────────────
@app.route('/api/agents', methods=['GET'])
def get_agents():
    conn = get_db()
    agents = conn.execute('''
        SELECT a.*,
               COUNT(c.id) as total_calls,
               AVG(c.overall_score) as avg_score
        FROM agents a
        LEFT JOIN calls c ON c.agent_name = a.name
        GROUP BY a.id
        ORDER BY a.name ASC
    ''').fetchall()
    conn.close()
    return jsonify([dict(a) for a in agents])

@app.route('/api/agents', methods=['POST'])
@require_admin
def add_agent():
    data = request.json
    name = data.get('name', '').strip()
    extension = data.get('extension', '').strip()
    if not name or not extension:
        return jsonify({'error': 'Name and extension are required'}), 400
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO agents (name, extension, email, photo, status) VALUES (?,?,?,?,?)',
        (name, extension, data.get('email',''), data.get('photo',''), data.get('status','active'))
    )
    agent_id = cursor.lastrowid
    conn.commit()
    agent = conn.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
    conn.close()
    return jsonify(dict(agent)), 201

@app.route('/api/agents/<int:agent_id>', methods=['PUT'])
@require_admin
def update_agent(agent_id):
    data = request.json
    conn = get_db()
    agent = conn.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return jsonify({'error': 'Agent not found'}), 404
    conn.execute(
        'UPDATE agents SET name=?, extension=?, email=?, photo=?, status=? WHERE id=?',
        (data.get('name', agent['name']), data.get('extension', agent['extension']),
         data.get('email', agent['email']), data.get('photo', agent['photo']),
         data.get('status', agent['status']), agent_id)
    )
    conn.commit()
    agent = conn.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
    conn.close()
    return jsonify(dict(agent))

@app.route('/api/agents/<int:agent_id>', methods=['DELETE'])
@require_admin
def delete_agent(agent_id):
    conn = get_db()
    conn.execute('DELETE FROM agents WHERE id=?', (agent_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ─── AI ANALYZE ENDPOINT ──────────────────────────────────────────────────────
@app.route('/api/analyze', methods=['POST'])
def analyze_call():
    """
    Accepts an audio file + agent name, runs full AI analysis.
    Your phone system calls this endpoint when a call ends.
    """
    try:
        from ai_engine import analyze_call as run_analysis

        agent_name = request.form.get('agent_name', 'Unknown')
        call_id = request.form.get('call_id', None)

        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided. Send audio file as multipart form field named "audio"'}), 400

        audio_file = request.files['audio']
        if audio_file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400

        # Save audio temporarily
        os.makedirs('uploads', exist_ok=True)
        safe_name = f"call_{call_id or 'temp'}_{int(__import__('time').time())}{os.path.splitext(audio_file.filename)[1]}"
        audio_path = os.path.join('uploads', safe_name)
        audio_file.save(audio_path)

        # Run AI analysis
        result = run_analysis(audio_path, agent_name, call_id)

        # Clean up audio file after analysis
        try:
            os.remove(audio_path)
        except:
            pass

        return jsonify({
            'success': True,
            'call_id': result['call_id'],
            'agent_name': result['agent_name'],
            'overall_score': result['overall_score'],
            'status': result['status'],
            'emotion': result['emotion'],
            'flags': result['flags'],
            'scorecard': result['scorecard'],
            'summary': result['summary']
        })

    except ImportError:
        return jsonify({'error': 'AI engine not available. Make sure ANTHROPIC_API_KEY and GEMINI_API_KEY are set in .env file'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/analyze/status', methods=['GET'])
def analyze_status():
    """Check if AI engine is ready"""
    anthropic_key = os.getenv('ANTHROPIC_API_KEY', '')
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    return jsonify({
        'ready': bool(anthropic_key and gemini_key and anthropic_key != 'your_anthropic_api_key_here'),
        'anthropic_configured': bool(anthropic_key and anthropic_key != 'your_anthropic_api_key_here'),
        'gemini_configured': bool(gemini_key and gemini_key != 'your_gemini_api_key_here'),
    })

# ─── SERVE FRONTEND ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'qa-dashboard.html')

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print('\n✅ VoiceGuard QA Server running!')
    print('📊 Open your dashboard: http://localhost:5000')
    print('🔐 Admin login: david / admin123')
    print('Press Ctrl+C to stop\n')
    app.run(debug=True, port=5000)
