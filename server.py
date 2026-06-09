from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import hashlib
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

load_dotenv()

app = Flask(__name__, static_folder='.')
app.secret_key = os.getenv('SECRET_KEY', 'voiceguard-secret-change-this')
CORS(app)

# ─── ADMIN CREDENTIALS ───────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'david')
ADMIN_PASSWORD = hashlib.sha256(os.getenv('ADMIN_PASSWORD', 'admin123').encode()).hexdigest()

# ─── API KEY ─────────────────────────────────────────────────────────────────
VOICEGUARD_API_KEY = os.getenv('VOICEGUARD_API_KEY', 'vg-change-this-secret-key')

# ─── DATABASE CONNECTION ──────────────────────────────────────────────────────
DATABASE_URL = os.getenv('DATABASE_URL', '')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = False
    return conn

# ─── DATABASE SETUP ──────────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS rules (
            id SERIAL PRIMARY KEY,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS agents (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            extension TEXT NOT NULL,
            email TEXT,
            photo TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id SERIAL PRIMARY KEY,
            call_id TEXT,
            agent_name TEXT,
            agent_extension TEXT,
            account_worked TEXT,
            line_issues TEXT DEFAULT 'none',
            duration TEXT,
            call_duration_seconds INTEGER DEFAULT 0,
            billed_minutes INTEGER DEFAULT 0,
            overall_score INTEGER,
            confidence INTEGER DEFAULT 100,
            emotion TEXT,
            emotion_delta TEXT,
            status TEXT,
            flags INTEGER DEFAULT 0,
            scorecard TEXT,
            transcript TEXT,
            recording_url TEXT,
            summary TEXT,
            call_dropped BOOLEAN DEFAULT FALSE,
            callback_made BOOLEAN DEFAULT FALSE,
            callback_call_id TEXT,
            requires_human_review BOOLEAN DEFAULT FALSE,
            human_review_reason TEXT,
            age_concern TEXT,
            coaching_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS rule_results (
            id SERIAL PRIMARY KEY,
            call_id TEXT,
            rule_id INTEGER,
            rule_description TEXT,
            category TEXT,
            severity TEXT,
            passed BOOLEAN,
            evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            action TEXT,
            user_name TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Insert default rules if empty
    c.execute('SELECT COUNT(*) FROM rules')
    count = c.fetchone()[0]
    if count == 0:
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
        c.executemany('INSERT INTO rules (description, category, severity) VALUES (%s,%s,%s)', default_rules)

    conn.commit()
    conn.close()
    print('✅ Database initialized successfully')

# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────
def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def require_api_key(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        data = request.json or {}
        key = data.get('api_key') or request.headers.get('X-API-Key', '')
        if key != VOICEGUARD_API_KEY:
            return jsonify({'error': 'Invalid or missing API key'}), 401
        return f(*args, **kwargs)
    return decorated

# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────
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

# ─── RULES API ────────────────────────────────────────────────────────────────
@app.route('/api/rules', methods=['GET'])
def get_rules():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM rules ORDER BY severity DESC, id ASC')
    rules = c.fetchall()
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
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute(
        'INSERT INTO rules (description, category, severity) VALUES (%s,%s,%s) RETURNING *',
        (description, category, severity)
    )
    rule = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(rule), 201

@app.route('/api/rules/<int:rule_id>', methods=['PUT'])
@require_admin
def update_rule(rule_id):
    data = request.json
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM rules WHERE id=%s', (rule_id,))
    rule = c.fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404
    c.execute(
        'UPDATE rules SET description=%s, category=%s, severity=%s, active=%s WHERE id=%s RETURNING *',
        (data.get('description', rule['description']),
         data.get('category', rule['category']),
         data.get('severity', rule['severity']),
         data.get('active', rule['active']), rule_id)
    )
    updated = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(updated)

@app.route('/api/rules/<int:rule_id>', methods=['DELETE'])
@require_admin
def delete_rule(rule_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rules WHERE id=%s', (rule_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/rules/<int:rule_id>/toggle', methods=['POST'])
@require_admin
def toggle_rule(rule_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM rules WHERE id=%s', (rule_id,))
    rule = c.fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404
    new_active = 0 if rule['active'] else 1
    c.execute('UPDATE rules SET active=%s WHERE id=%s RETURNING *', (new_active, rule_id))
    updated = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(updated)

# ─── AGENTS API ───────────────────────────────────────────────────────────────
@app.route('/api/agents', methods=['GET'])
def get_agents():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT a.*,
               COUNT(c.id) as total_calls,
               AVG(c.overall_score) as avg_score
        FROM agents a
        LEFT JOIN calls c ON c.agent_name = a.name
        GROUP BY a.id
        ORDER BY a.name ASC
    ''')
    agents = c.fetchall()
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
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute(
        'INSERT INTO agents (name, extension, email, photo, status) VALUES (%s,%s,%s,%s,%s) RETURNING *',
        (name, extension, data.get('email',''), data.get('photo',''), data.get('status','active'))
    )
    agent = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(agent), 201

@app.route('/api/agents/<int:agent_id>', methods=['PUT'])
@require_admin
def update_agent(agent_id):
    data = request.json
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM agents WHERE id=%s', (agent_id,))
    agent = c.fetchone()
    if not agent:
        conn.close()
        return jsonify({'error': 'Agent not found'}), 404
    c.execute(
        'UPDATE agents SET name=%s, extension=%s, email=%s, photo=%s, status=%s WHERE id=%s RETURNING *',
        (data.get('name', agent['name']), data.get('extension', agent['extension']),
         data.get('email', agent['email']), data.get('photo', agent['photo']),
         data.get('status', agent['status']), agent_id)
    )
    updated = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(updated)

@app.route('/api/agents/<int:agent_id>', methods=['DELETE'])
@require_admin
def delete_agent(agent_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM agents WHERE id=%s', (agent_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ─── CALLS API ────────────────────────────────────────────────────────────────
@app.route('/api/calls', methods=['GET'])
def get_calls():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM calls ORDER BY created_at DESC LIMIT 100')
    calls = c.fetchall()
    conn.close()
    return jsonify([dict(c) for c in calls])

@app.route('/api/calls/<call_id>', methods=['GET'])
def get_call(call_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM calls WHERE call_id=%s', (call_id,))
    call = c.fetchone()
    if not call:
        conn.close()
        return jsonify({'error': 'Call not found'}), 404
    # Get rule results for this call
    c.execute('SELECT * FROM rule_results WHERE call_id=%s ORDER BY severity DESC', (call_id,))
    rule_results = c.fetchall()
    conn.close()
    result = dict(call)
    result['rule_results'] = [dict(r) for r in rule_results]
    return jsonify(result)

# ─── STATS API ────────────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT COUNT(*) as total FROM calls')
    total_calls = c.fetchone()['total']
    c.execute('SELECT AVG(overall_score) as avg FROM calls')
    avg_score = c.fetchone()['avg'] or 0
    c.execute("SELECT COUNT(*) as total FROM calls WHERE status='Critical'")
    critical_flags = c.fetchone()['total']
    c.execute('SELECT COUNT(DISTINCT agent_name) as total FROM calls WHERE overall_score < 70')
    needs_coaching = c.fetchone()['total']
    c.execute('SELECT COUNT(*) as total FROM rules WHERE active=1')
    active_rules = c.fetchone()['total']
    c.execute('SELECT COUNT(*) as total FROM agents WHERE status=%s', ('active',))
    active_agents = c.fetchone()['total']
    conn.close()
    return jsonify({
        'total_calls': total_calls,
        'avg_score': round(float(avg_score), 1),
        'critical_flags': critical_flags,
        'needs_coaching': needs_coaching,
        'active_rules': active_rules,
        'active_agents': active_agents
    })

@app.route('/api/analytics/billing', methods=['GET'])
def get_billing():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT 
            agent_name,
            agent_extension,
            COUNT(*) as total_calls,
            SUM(call_duration_seconds) as total_seconds,
            SUM(billed_minutes) as total_billed_minutes,
            ROUND(SUM(call_duration_seconds) / 60.0, 1) as actual_minutes,
            SUM(billed_minutes) - ROUND(SUM(call_duration_seconds) / 60.0, 1) as billing_difference
        FROM calls
        WHERE call_duration_seconds > 0
        GROUP BY agent_name, agent_extension
        ORDER BY total_billed_minutes DESC
    ''')
    results = c.fetchall()
    
    # Team totals
    c.execute('''
        SELECT 
            SUM(call_duration_seconds) as total_seconds,
            SUM(billed_minutes) as total_billed_minutes,
            COUNT(*) as total_calls
        FROM calls
        WHERE call_duration_seconds > 0
    ''')
    totals = c.fetchone()
    conn.close()
    
    return jsonify({
        'agents': [dict(r) for r in results],
        'totals': dict(totals) if totals else {}
    })

@app.route('/api/analytics/trends', methods=['GET'])
def get_trends():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT DATE(created_at) as date,
               COUNT(*) as total_calls,
               AVG(overall_score) as avg_score,
               SUM(CASE WHEN status='Critical' THEN 1 ELSE 0 END) as critical_count
        FROM calls
        WHERE created_at >= NOW() - INTERVAL '30 days'
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    ''')
    trends = c.fetchall()
    conn.close()
    return jsonify([dict(t) for t in trends])

@app.route('/api/analytics/rule-stats', methods=['GET'])
def get_rule_stats():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT rule_description, category, severity,
               COUNT(*) as total_checks,
               SUM(CASE WHEN passed THEN 1 ELSE 0 END) as passed_count,
               SUM(CASE WHEN NOT passed THEN 1 ELSE 0 END) as failed_count,
               ROUND(100.0 * SUM(CASE WHEN passed THEN 1 ELSE 0 END) / COUNT(*), 1) as pass_rate
        FROM rule_results
        GROUP BY rule_description, category, severity
        ORDER BY failed_count DESC
        LIMIT 20
    ''')
    stats = c.fetchall()
    conn.close()
    return jsonify([dict(s) for s in stats])

@app.route('/api/analytics/agent-trends', methods=['GET'])
def get_agent_trends():
    agent_name = request.args.get('agent')
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    if agent_name:
        c.execute('''
            SELECT DATE(created_at) as date,
                   AVG(overall_score) as avg_score,
                   COUNT(*) as total_calls
            FROM calls
            WHERE agent_name=%s AND created_at >= NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date ASC
        ''', (agent_name,))
    else:
        c.execute('''
            SELECT agent_name,
                   AVG(overall_score) as avg_score,
                   COUNT(*) as total_calls,
                   SUM(CASE WHEN status='Critical' THEN 1 ELSE 0 END) as critical_count
            FROM calls
            GROUP BY agent_name
            ORDER BY avg_score DESC
        ''')
    results = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in results])

# ─── AI ANALYZE ENDPOINT (ASYNC QUEUE) ───────────────────────────────────────
@app.route('/api/analyze', methods=['POST'])
@require_api_key
def analyze_call():
    try:
        import redis as redis_lib

        data = request.json
        agent_name = data.get('agent_name', 'Unknown')
        agent_extension = data.get('agent_extension', '')
        call_id = data.get('call_id', f"CALL-{int(time.time())}")
        recording_url = data.get('recording_url', '')
        call_duration_seconds = data.get('call_duration_seconds', 0)
        billed_minutes = data.get('billed_minutes', 0)
        caller_id = data.get('caller_id', '')
        call_dropped = data.get('call_dropped', False)

        # Format duration as MM:SS for display
        if call_duration_seconds:
            mins = call_duration_seconds // 60
            secs = call_duration_seconds % 60
            duration_display = f"{mins}:{secs:02d}"
        else:
            duration_display = '--'

        if not recording_url:
            return jsonify({'error': 'recording_url is required'}), 400
        if not agent_name or agent_name == 'Unknown':
            return jsonify({'error': 'agent_name is required'}), 400

        # Insert pending record immediately
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO calls (call_id, agent_name, agent_extension, caller_id, recording_url,
                             call_duration_seconds, billed_minutes, duration, call_dropped, status, overall_score, flags)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        ''', (call_id, agent_name, agent_extension, caller_id, recording_url,
              call_duration_seconds, billed_minutes, duration_display, call_dropped, 'Processing', 0, 0))
        conn.commit()
        conn.close()

        # Add to Redis queue
        REDIS_URL = os.getenv('REDIS_URL', '')
        if REDIS_URL:
            r = redis_lib.from_url(REDIS_URL, decode_responses=True)
            job = json.dumps({
                'call_id': call_id,
                'agent_name': agent_name,
                'agent_extension': agent_extension,
                'recording_url': recording_url,
                'call_duration_seconds': call_duration_seconds,
                'billed_minutes': billed_minutes,
                'caller_id': caller_id,
                'call_dropped': call_dropped
            })
            r.lpush('voiceguard:calls', job)
            return jsonify({
                'success': True,
                'call_id': call_id,
                'status': 'Processing',
                'message': 'Call received and queued for analysis. Check dashboard for results in 1-2 minutes.'
            })
        else:
            # Fallback: process synchronously if no Redis
            from ai_engine import analyze_call as run_analysis
            import urllib.request

            url_path = recording_url.split('?')[0]
            ext = os.path.splitext(url_path)[1].lower()
            if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
                ext = '.wav'

            UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            audio_path = os.path.join(UPLOAD_DIR, f"call_{call_id}_{int(time.time())}{ext}")

            try:
                urllib.request.urlretrieve(recording_url, audio_path)
                result = run_analysis(audio_path, agent_name, call_id, caller_id=caller_id, call_dropped=call_dropped)
            finally:
                try:
                    os.remove(audio_path)
                except:
                    pass

            conn = get_db()
            c = conn.cursor()
            c.execute('''
                UPDATE calls SET duration=%s, overall_score=%s, confidence=%s, emotion=%s, 
                    status=%s, flags=%s, scorecard=%s, transcript=%s, summary=%s,
                    emotion_delta=%s, requires_human_review=%s, human_review_reason=%s,
                    age_concern=%s, coaching_notes=%s, call_dropped=%s,
                    callback_made=%s, callback_call_id=%s
                WHERE call_id=%s
            ''', (
                result.get('duration', '--'), result['overall_score'],
                result.get('confidence', 100), result['emotion'],
                result['status'], result['flags'],
                json.dumps(result.get('scorecard', {})),
                result.get('transcript', ''), result.get('summary', ''),
                json.dumps(result.get('emotion_delta', {})),
                result.get('requires_human_review', False),
                result.get('human_review_reason', ''),
                json.dumps(result.get('age_concern', {})),
                result.get('coaching_notes', ''),
                result.get('call_dropped', False),
                result.get('is_callback', False),
                result.get('original_call_id', ''),
                call_id
            ))
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'call_id': result['call_id'],
                'overall_score': result['overall_score'],
                'status': result['status'],
                'emotion': result['emotion'],
                'flags': result['flags'],
                'summary': result.get('summary', '')
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/analyze/status', methods=['GET'])
def analyze_status():
    anthropic_key = os.getenv('ANTHROPIC_API_KEY', '')
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    db_url = os.getenv('DATABASE_URL', '')
    db_ok = False
    try:
        conn = get_db()
        conn.close()
        db_ok = True
    except:
        pass
    return jsonify({
        'ready': bool(anthropic_key and gemini_key and db_ok and anthropic_key != 'your_anthropic_api_key_here'),
        'anthropic_configured': bool(anthropic_key and anthropic_key != 'your_anthropic_api_key_here'),
        'gemini_configured': bool(gemini_key and gemini_key != 'your_gemini_api_key_here'),
        'database_connected': db_ok
    })

# ─── SERVE FRONTEND ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'qa-dashboard.html')

# ─── INITIALIZE ───────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as e:
    print(f'⚠️ Database init warning: {e}')

if __name__ == '__main__':
    print('\n✅ VoiceGuard QA Server running!')
    print('📊 Open your dashboard: http://localhost:5000')
    app.run(debug=True, port=5000)
