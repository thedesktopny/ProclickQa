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

load_dotenv()

app = Flask(__name__, static_folder='.')
app.secret_key = os.getenv('SECRET_KEY', 'voiceguard-secret-change-this')
CORS(app)

ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'david')
ADMIN_PASSWORD = hashlib.sha256(os.getenv('ADMIN_PASSWORD', 'admin123').encode()).hexdigest()
VOICEGUARD_API_KEY = os.getenv('VOICEGUARD_API_KEY', 'vg-change-this-secret-key')
DATABASE_URL = os.getenv('DATABASE_URL', '')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

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
            call_id TEXT UNIQUE,
            agent_name TEXT,
            agent_extension TEXT,
            caller_id TEXT,
            customer_account_id TEXT,
            account_name TEXT,
            duration TEXT,
            call_duration_seconds INTEGER DEFAULT 0,
            billed_minutes INTEGER DEFAULT 0,
            overall_score INTEGER,
            confidence INTEGER DEFAULT 100,
            emotion TEXT,
            emotion_delta TEXT,
            status TEXT DEFAULT 'Processing',
            flags INTEGER DEFAULT 0,
            scorecard TEXT,
            transcript TEXT,
            recording_url TEXT,
            summary TEXT,
            call_end_first TEXT DEFAULT 'customer',
            agent_qos_tx TEXT DEFAULT 'Good',
            agent_qos_rx TEXT DEFAULT 'Good',
            customer_qos_tx TEXT DEFAULT 'Good',
            customer_qos_rx TEXT DEFAULT 'Good',
            call_notes TEXT,
            notes_score INTEGER,
            notes_feedback TEXT,
            call_dropped BOOLEAN DEFAULT FALSE,
            callback_made BOOLEAN DEFAULT FALSE,
            callback_call_id TEXT,
            requires_human_review BOOLEAN DEFAULT FALSE,
            human_review_reason TEXT,
            age_concern TEXT,
            coaching_notes TEXT,
            positive_highlights TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS rule_results (
            id SERIAL PRIMARY KEY,
            call_id TEXT,
            rule_description TEXT,
            category TEXT,
            severity TEXT,
            passed BOOLEAN,
            confidence INTEGER DEFAULT 100,
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

    # Migration — add new columns if they don't exist
    migrations = [
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS caller_id TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS agent_qos_tx TEXT DEFAULT 'Good'",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS agent_qos_rx TEXT DEFAULT 'Good'",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS customer_qos_tx TEXT DEFAULT 'Good'",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS customer_qos_rx TEXT DEFAULT 'Good'",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS notes_score INTEGER",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS notes_feedback TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS emotion_delta TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS requires_human_review BOOLEAN DEFAULT FALSE",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS human_review_reason TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS age_concern TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS coaching_notes TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS positive_highlights TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS callback_made BOOLEAN DEFAULT FALSE",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS callback_call_id TEXT",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS confidence INTEGER DEFAULT 100",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS summary TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS agents_name_unique ON agents (name)",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            pass
    c.execute('SELECT COUNT(*) FROM rules')
    if c.fetchone()[0] == 0:
        default_rules = [
            ('Agent must never use inappropriate, offensive, or sexual language of any kind', 'Forbidden Words', 'Critical'),
            ('Agent must read the total price including shipping out loud before placing any order', 'Compliance', 'Critical'),
            ('Agent must receive explicit verbal confirmation from customer before completing any purchase', 'Compliance', 'Critical'),
            ('Agent must verify the customer identity at the start of every call before accessing any account', 'Compliance', 'Critical'),
            ('Agent must verbally confirm "I have logged out of your account" before ending any call where account access occurred', 'Compliance', 'Critical'),
            ('Agent should not overuse the word "sir" — using it too frequently sounds robotic', 'Behavior', 'Warning'),
            ('If a customer sounds frustrated or upset, agent must acknowledge their feelings before continuing', 'Behavior', 'Warning'),
            ('Agent must not discuss working outside of Proclick or solicit personal contact with customers', 'Conduct', 'Critical'),
            ('Agent must not make promises or guarantees without authorization', 'Compliance', 'Warning'),
            ('Agent must ask "Is there anything else I can help you with today?" before ending the call', 'Required Phrases', 'Info'),
            ('If a customer mentions a competitor, agent must not speak negatively about them', 'Behavior', 'Info'),
            ('Agent must write detailed and accurate call notes after every call', 'Documentation', 'Warning'),
        ]
        c.executemany('INSERT INTO rules (description, category, severity) VALUES (%s,%s,%s)', default_rules)

    conn.commit()
    conn.close()
    print('✅ Database initialized')

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

# ─── RULES ────────────────────────────────────────────────────────────────────
@app.route('/api/rules', methods=['GET'])
def get_rules():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM rules ORDER BY severity DESC, id ASC')
    rules = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rules)

@app.route('/api/rules', methods=['POST'])
@require_admin
def add_rule():
    data = request.json
    description = data.get('description', '').strip()
    if not description:
        return jsonify({'error': 'Description required'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('INSERT INTO rules (description, category, severity) VALUES (%s,%s,%s) RETURNING *',
              (description, data.get('category','Behavior'), data.get('severity','Warning')))
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
        return jsonify({'error': 'Not found'}), 404
    c.execute('UPDATE rules SET description=%s, category=%s, severity=%s, active=%s WHERE id=%s RETURNING *',
              (data.get('description', rule['description']), data.get('category', rule['category']),
               data.get('severity', rule['severity']), data.get('active', rule['active']), rule_id))
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
        return jsonify({'error': 'Not found'}), 404
    new_active = 0 if rule['active'] else 1
    c.execute('UPDATE rules SET active=%s WHERE id=%s RETURNING *', (new_active, rule_id))
    updated = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(updated)

# ─── AGENTS ───────────────────────────────────────────────────────────────────
@app.route('/api/agents', methods=['GET'])
def get_agents():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT a.*, COUNT(c.id) as total_calls, AVG(c.overall_score) as avg_score
        FROM agents a
        LEFT JOIN calls c ON c.agent_name = a.name
        GROUP BY a.id ORDER BY a.name ASC
    ''')
    agents = [dict(a) for a in c.fetchall()]
    conn.close()
    return jsonify(agents)

@app.route('/api/agents', methods=['POST'])
@require_admin
def add_agent():
    data = request.json
    if not data.get('name') or not data.get('extension'):
        return jsonify({'error': 'Name and extension required'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('INSERT INTO agents (name, extension, email, photo, status) VALUES (%s,%s,%s,%s,%s) RETURNING *',
              (data['name'], data['extension'], data.get('email',''), data.get('photo',''), data.get('status','active')))
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
        return jsonify({'error': 'Not found'}), 404
    c.execute('UPDATE agents SET name=%s, extension=%s, email=%s, photo=%s, status=%s WHERE id=%s RETURNING *',
              (data.get('name', agent['name']), data.get('extension', agent['extension']),
               data.get('email', agent['email']), data.get('photo', agent['photo']),
               data.get('status', agent['status']), agent_id))
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

# ─── CALLS ────────────────────────────────────────────────────────────────────
@app.route('/api/calls', methods=['GET'])
def get_calls():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM calls ORDER BY created_at DESC LIMIT 100')
    calls = [dict(c) for c in c.fetchall()]
    conn.close()
    return jsonify(calls)

@app.route('/api/calls/<call_id>', methods=['GET'])
def get_call(call_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM calls WHERE call_id=%s', (call_id,))
    call = c.fetchone()
    if not call:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    c.execute('SELECT * FROM rule_results WHERE call_id=%s ORDER BY severity DESC', (call_id,))
    rule_results = [dict(r) for r in c.fetchall()]
    conn.close()
    result = dict(call)
    result['rule_results'] = rule_results
    return jsonify(result)

# ─── STATS ────────────────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT COUNT(*) as v FROM calls WHERE status != 'Processing'")
    total_calls = c.fetchone()['v']
    c.execute("SELECT AVG(overall_score) as v FROM calls WHERE overall_score > 0")
    avg_score = c.fetchone()['v'] or 0
    c.execute("SELECT COUNT(*) as v FROM calls WHERE status='Critical'")
    critical_flags = c.fetchone()['v']
    c.execute("SELECT COUNT(DISTINCT agent_name) as v FROM calls WHERE overall_score < 70 AND overall_score > 0")
    needs_coaching = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM rules WHERE active=1")
    active_rules = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM agents WHERE status='active'")
    active_agents = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM calls WHERE requires_human_review=true AND status != 'Processing'")
    needs_review = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM calls WHERE call_end_first='drop' AND callback_made=false AND status != 'Processing'")
    unresolved_drops = c.fetchone()['v']
    conn.close()
    return jsonify({
        'total_calls': total_calls,
        'avg_score': round(float(avg_score), 1),
        'critical_flags': critical_flags,
        'needs_coaching': needs_coaching,
        'active_rules': active_rules,
        'active_agents': active_agents,
        'needs_human_review': needs_review,
        'unresolved_drops': unresolved_drops
    })

# ─── ANALYTICS ────────────────────────────────────────────────────────────────
@app.route('/api/analytics/trends', methods=['GET'])
def get_trends():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT DATE(created_at) as date,
               COUNT(*) as total_calls,
               AVG(overall_score) as avg_score,
               SUM(CASE WHEN status='Critical' THEN 1 ELSE 0 END) as critical_count
        FROM calls WHERE created_at >= NOW() - INTERVAL '30 days'
        AND overall_score > 0
        GROUP BY DATE(created_at) ORDER BY date ASC
    ''')
    trends = [dict(t) for t in c.fetchall()]
    conn.close()
    return jsonify(trends)

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
        ORDER BY failed_count DESC LIMIT 20
    ''')
    stats = [dict(s) for s in c.fetchall()]
    conn.close()
    return jsonify(stats)

@app.route('/api/analytics/agent-stats', methods=['GET'])
def get_agent_stats():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT agent_name, agent_extension,
               COUNT(*) as total_calls,
               AVG(overall_score) as avg_score,
               AVG(notes_score) as avg_notes_score,
               SUM(CASE WHEN status='Critical' THEN 1 ELSE 0 END) as critical_count,
               SUM(CASE WHEN requires_human_review THEN 1 ELSE 0 END) as review_count,
               SUM(CASE WHEN call_end_first='agent' THEN 1 ELSE 0 END) as agent_ended_count,
               SUM(CASE WHEN line_issues='agent' THEN 1 ELSE 0 END) as line_issues_count
        FROM calls WHERE overall_score > 0
        GROUP BY agent_name, agent_extension
        ORDER BY avg_score DESC
    ''')
    stats = [dict(s) for s in c.fetchall()]
    conn.close()
    return jsonify(stats)

@app.route('/api/analytics/billing', methods=['GET'])
def get_billing():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT agent_name, agent_extension,
               COUNT(*) as total_calls,
               SUM(call_duration_seconds) as total_seconds,
               SUM(billed_minutes) as total_billed_minutes,
               ROUND(SUM(call_duration_seconds) / 60.0, 1) as actual_minutes,
               SUM(billed_minutes) - ROUND(SUM(call_duration_seconds) / 60.0, 1) as billing_difference
        FROM calls WHERE call_duration_seconds > 0
        GROUP BY agent_name, agent_extension
        ORDER BY total_billed_minutes DESC
    ''')
    agents = [dict(r) for r in c.fetchall()]
    c.execute('''
        SELECT SUM(call_duration_seconds) as total_seconds,
               SUM(billed_minutes) as total_billed_minutes,
               COUNT(*) as total_calls
        FROM calls WHERE call_duration_seconds > 0
    ''')
    totals = dict(c.fetchone())
    conn.close()
    return jsonify({'agents': agents, 'totals': totals})

@app.route('/api/analytics/notes-quality', methods=['GET'])
def get_notes_quality():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT agent_name,
               COUNT(*) as total_calls,
               SUM(CASE WHEN call_notes IS NULL OR call_notes = '' THEN 1 ELSE 0 END) as missing_notes,
               AVG(notes_score) as avg_notes_score,
               SUM(CASE WHEN notes_score < 60 THEN 1 ELSE 0 END) as poor_notes_count
        FROM calls WHERE overall_score > 0
        GROUP BY agent_name
        ORDER BY avg_notes_score ASC
    ''')
    stats = [dict(s) for s in c.fetchall()]
    conn.close()
    return jsonify(stats)

@app.route('/api/analytics/drops', methods=['GET'])
def get_drops():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT * FROM calls
        WHERE call_end_first = 'drop'
        ORDER BY created_at DESC LIMIT 50
    ''')
    drops = [dict(d) for d in c.fetchall()]
    conn.close()
    return jsonify(drops)

# ─── ANALYZE ENDPOINT ─────────────────────────────────────────────────────────
@app.route('/api/analyze', methods=['POST'])
@require_api_key
def analyze_call():
    try:
        data = request.json
        agent_name = data.get('agent_name', 'Unknown')
        agent_extension = data.get('agent_extension', '')
        call_id = data.get('call_id', f"CALL-{int(time.time())}")
        recording_url = data.get('recording_url', '')
        call_duration_seconds = data.get('call_duration_seconds', 0)
        billed_minutes = data.get('billed_minutes', 0)
        caller_id = data.get('caller_id', '')
        customer_account_id = data.get('customer_account_id', '')
        account_name = data.get('account_name', '')
        call_end_first = data.get('call_end_first', 'customer')
        agent_qos_tx = data.get('agent_qos_tx', 'Good')
        agent_qos_rx = data.get('agent_qos_rx', 'Good')
        customer_qos_tx = data.get('customer_qos_tx', 'Good')
        customer_qos_rx = data.get('customer_qos_rx', 'Good')
        call_notes = data.get('call_notes', '')

        # Clean recording URL — strip trailing colons, spaces, or other invalid chars
        recording_url = recording_url.strip().rstrip(':').rstrip('/')
        if not agent_name or agent_name == 'Unknown':
            return jsonify({'error': 'agent_name is required'}), 400

        # Format duration
        if call_duration_seconds:
            mins = call_duration_seconds // 60
            secs = call_duration_seconds % 60
            duration_display = f"{mins}:{secs:02d}"
        else:
            duration_display = '--'

        call_dropped = (call_end_first == 'drop')

        # Auto-create agent if not exists
        if agent_name and agent_name != 'Unknown':
            try:
                conn_a = get_db()
                c_a = conn_a.cursor()
                c_a.execute('''
                    INSERT INTO agents (name, extension, status)
                    VALUES (%s, %s, 'active')
                    ON CONFLICT (name) DO UPDATE SET extension = EXCLUDED.extension
                ''', (agent_name.strip(), agent_extension.strip() or '—'))
                conn_a.commit()
                conn_a.close()
            except Exception:
                pass

        # Insert pending record immediately
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO calls (call_id, agent_name, agent_extension, caller_id, customer_account_id,
                             account_name, recording_url, call_duration_seconds, billed_minutes,
                             duration, call_end_first, agent_qos_tx, agent_qos_rx, 
                             customer_qos_tx, customer_qos_rx, call_notes, call_dropped,
                             status, overall_score, flags)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (call_id) DO NOTHING
        ''', (call_id, agent_name, agent_extension, caller_id, customer_account_id, account_name,
              recording_url, call_duration_seconds, billed_minutes, duration_display,
              call_end_first, agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx,
              call_notes, call_dropped, 'Processing', 0, 0))
        conn.commit()
        conn.close()

        # Check for callback (same customer_account_id, drop within 10 min)
        if customer_account_id and call_dropped:
            try:
                conn = get_db()
                c = conn.cursor(cursor_factory=RealDictCursor)
                c.execute('''
                    SELECT call_id FROM calls
                    WHERE customer_account_id = %s
                    AND call_end_first = 'drop'
                    AND call_id != %s
                    AND created_at >= NOW() - INTERVAL '10 minutes'
                    ORDER BY created_at DESC LIMIT 1
                ''', (customer_account_id, call_id))
                prev_drop = c.fetchone()
                if prev_drop:
                    c.execute("UPDATE calls SET callback_made=true, callback_call_id=%s WHERE call_id=%s",
                              (call_id, prev_drop['call_id']))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Callback check warning: {e}")

        # Queue or process synchronously
        REDIS_URL = os.getenv('REDIS_URL', '')
        if REDIS_URL:
            import redis as redis_lib
            r = redis_lib.from_url(REDIS_URL, decode_responses=True)
            job = json.dumps({
                'call_id': call_id, 'agent_name': agent_name,
                'agent_extension': agent_extension, 'recording_url': recording_url,
                'call_duration_seconds': call_duration_seconds, 'billed_minutes': billed_minutes,
                'caller_id': caller_id, 'customer_account_id': customer_account_id,
                'account_name': account_name, 'call_end_first': call_end_first,
                'agent_qos_tx': agent_qos_tx, 'agent_qos_rx': agent_qos_rx,
                'customer_qos_tx': customer_qos_tx, 'customer_qos_rx': customer_qos_rx,
                'call_notes': call_notes, 'call_dropped': call_dropped
            })
            r.lpush('voiceguard:calls', job)
            return jsonify({
                'success': True, 'call_id': call_id, 'status': 'Processing',
                'message': 'Call received and queued for analysis. Results in 1-2 minutes.'
            })
        else:
            # Synchronous fallback
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
                result = run_analysis(audio_path, agent_name, call_id,
                                    call_end_first=call_end_first,
                                    call_notes=call_notes,
                                    account_name=account_name,
                                    agent_qos_tx=agent_qos_tx, agent_qos_rx=agent_qos_rx,
                                    customer_qos_tx=customer_qos_tx, customer_qos_rx=customer_qos_rx)
            finally:
                try: os.remove(audio_path)
                except: pass

            conn = get_db()
            c = conn.cursor()
            c.execute('''
                UPDATE calls SET duration=%s, overall_score=%s, confidence=%s,
                    emotion=%s, status=%s, flags=%s, scorecard=%s, transcript=%s,
                    summary=%s, emotion_delta=%s, requires_human_review=%s,
                    human_review_reason=%s, age_concern=%s, coaching_notes=%s,
                    positive_highlights=%s, call_dropped=%s, notes_score=%s,
                    notes_feedback=%s
                WHERE call_id=%s
            ''', (
                result.get('duration','--'), result['overall_score'],
                result.get('confidence',100), result['emotion'], result['status'],
                result['flags'], json.dumps(result.get('scorecard',{})),
                result.get('transcript',''), result.get('summary',''),
                json.dumps(result.get('emotion_delta',{})),
                result.get('requires_human_review',False),
                result.get('human_review_reason',''),
                json.dumps(result.get('age_concern',{})),
                result.get('coaching_notes',''),
                result.get('positive_highlights',''),
                result.get('call_dropped', False),
                result.get('notes_score', 0),
                result.get('notes_feedback',''),
                call_id
            ))

            # Save rule results
            for rr in result.get('scorecard',{}).get('rules_evaluation',[]):
                c.execute('''INSERT INTO rule_results
                    (call_id, rule_description, category, severity, passed, confidence, evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    (call_id, rr.get('rule',''), rr.get('category',''),
                     rr.get('severity',''), rr.get('passed',False),
                     rr.get('confidence',100), rr.get('evidence','')))
            conn.commit()
            conn.close()

            return jsonify({
                'success': True, 'call_id': result['call_id'],
                'overall_score': result['overall_score'],
                'status': result['status'], 'emotion': result['emotion'],
                'flags': result['flags'], 'summary': result.get('summary','')
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── RETRY STUCK CALLS ────────────────────────────────────────────────────────
@app.route('/api/retry-stuck', methods=['POST'])
@require_admin
def retry_stuck_calls():
    """
    Retries calls stuck in Processing status.
    Throttled — max 10 calls per batch, 30s delay between each.
    Runs in background so response is instant.
    """
    import threading

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT call_id, agent_name, agent_extension, recording_url,
               call_duration_seconds, billed_minutes, caller_id,
               customer_account_id, account_name, call_end_first,
               agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx,
               call_notes, call_dropped
        FROM calls
        WHERE status = 'Processing'
        AND overall_score = 0
        AND created_at < NOW() - INTERVAL '5 minutes'
        ORDER BY created_at ASC
        LIMIT 10
    ''')
    stuck_calls = [dict(c) for c in c.fetchall()]
    conn.close()

    if not stuck_calls:
        return jsonify({'message': 'No stuck calls found', 'count': 0})

    def process_batch():
        from ai_engine import analyze_call as run_analysis
        import urllib.request

        for call in stuck_calls:
            try:
                call_id = call['call_id']
                recording_url = (call['recording_url'] or '').strip().rstrip(':').rstrip('/')

                if not recording_url:
                    continue

                url_path = recording_url.split('?')[0]
                ext = os.path.splitext(url_path)[1].lower()
                if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
                    ext = '.wav'

                UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                audio_path = os.path.join(UPLOAD_DIR, f"retry_{call_id}_{int(time.time())}{ext}")

                print(f"[Retry] Processing call {call_id}")

                try:
                    urllib.request.urlretrieve(recording_url, audio_path)
                    result = run_analysis(
                        audio_path,
                        call['agent_name'] or 'Unknown',
                        call_id,
                        call_end_first=call.get('call_end_first') or 'customer',
                        call_notes=call.get('call_notes') or '',
                        account_name=call.get('account_name') or '',
                        agent_qos_tx=call.get('agent_qos_tx') or 'Good',
                        agent_qos_rx=call.get('agent_qos_rx') or 'Good',
                        customer_qos_tx=call.get('customer_qos_tx') or 'Good',
                        customer_qos_rx=call.get('customer_qos_rx') or 'Good',
                        call_dropped=call.get('call_dropped') or False
                    )

                    conn2 = get_db()
                    c2 = conn2.cursor()
                    c2.execute('''
                        UPDATE calls SET duration=%s, overall_score=%s, confidence=%s,
                            emotion=%s, status=%s, flags=%s, scorecard=%s, transcript=%s,
                            summary=%s, emotion_delta=%s, requires_human_review=%s,
                            human_review_reason=%s, age_concern=%s, coaching_notes=%s,
                            positive_highlights=%s, call_dropped=%s, notes_score=%s,
                            notes_feedback=%s
                        WHERE call_id=%s
                    ''', (
                        result.get('duration','--'), result['overall_score'],
                        result.get('confidence',100), result['emotion'],
                        result['status'], result['flags'],
                        json.dumps(result.get('scorecard',{})),
                        result.get('transcript',''), result.get('summary',''),
                        json.dumps(result.get('emotion_delta',{})),
                        result.get('requires_human_review',False),
                        result.get('human_review_reason',''),
                        json.dumps(result.get('age_concern',{})),
                        result.get('coaching_notes',''),
                        result.get('positive_highlights',''),
                        result.get('call_dropped',False),
                        result.get('notes_score',0),
                        result.get('notes_feedback',''),
                        call_id
                    ))
                    for rr in result.get('scorecard',{}).get('rules_evaluation',[]):
                        c2.execute('''INSERT INTO rule_results
                            (call_id, rule_description, category, severity, passed, confidence, evidence)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                            (call_id, rr.get('rule',''), rr.get('category',''),
                             rr.get('severity',''), rr.get('passed',False),
                             rr.get('confidence',100), rr.get('evidence','')))
                    conn2.commit()
                    conn2.close()
                    print(f"[Retry] ✅ Call {call_id} scored: {result['overall_score']}%")

                finally:
                    try: os.remove(audio_path)
                    except: pass

                # Wait 30 seconds between calls to avoid overloading
                time.sleep(30)

            except Exception as e:
                print(f"[Retry] ❌ Failed {call.get('call_id')}: {e}")
                try:
                    conn3 = get_db()
                    c3 = conn3.cursor()
                    c3.execute("UPDATE calls SET status='Failed' WHERE call_id=%s", (call.get('call_id'),))
                    conn3.commit()
                    conn3.close()
                except: pass
                # Still wait before next call even on failure
                time.sleep(10)

    # Run in background thread
    thread = threading.Thread(target=process_batch, daemon=True)
    thread.start()

    return jsonify({
        'message': f'Retry started for {len(stuck_calls)} stuck calls. Processing one at a time with 30s delay.',
        'count': len(stuck_calls),
        'call_ids': [c['call_id'] for c in stuck_calls]
    })

@app.route('/api/analyze/status', methods=['GET'])
def analyze_status():
    anthropic_key = os.getenv('ANTHROPIC_API_KEY', '')
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    db_ok = False
    try:
        conn = get_db(); conn.close(); db_ok = True
    except: pass
    return jsonify({
        'ready': bool(anthropic_key and gemini_key and db_ok and 'your_' not in anthropic_key),
        'anthropic_configured': bool(anthropic_key and 'your_' not in anthropic_key),
        'gemini_configured': bool(gemini_key and 'your_' not in gemini_key),
        'database_connected': db_ok
    })

@app.route('/api/test-analyze', methods=['GET'])
def test_analyze():
    """Test each component of the AI pipeline individually."""
    results = {}

    # Test 1: Database
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM calls')
        count = c.fetchone()[0]
        conn.close()
        results['database'] = f'OK — {count} calls'
    except Exception as e:
        results['database'] = f'FAIL: {str(e)}'

    # Test 2: Anthropic / Claude
    try:
        import anthropic as anthropic_lib
        client = anthropic_lib.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=20,
            messages=[{'role':'user','content':'Reply with just the word OK'}]
        )
        results['claude'] = f'OK — {msg.content[0].text.strip()}'
    except Exception as e:
        results['claude'] = f'FAIL: {str(e)}'

    # Test 3: Gemini
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
        model = genai.GenerativeModel('gemini-2.5-flash')
        resp = model.generate_content('Reply with just the word OK')
        results['gemini'] = f'OK — {resp.text.strip()}'
    except Exception as e:
        results['gemini'] = f'FAIL: {str(e)}'

    # Test 4: Can we download a recording?
    try:
        import urllib.request
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT recording_url, call_id FROM calls WHERE recording_url IS NOT NULL AND recording_url != '' LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            url = row[0].strip().rstrip(':').rstrip('/')
            req = urllib.request.Request(url, method='HEAD')
            urllib.request.urlopen(req, timeout=10)
            results['audio_url'] = f'OK — accessible: {url[-40:]}'
        else:
            results['audio_url'] = 'No calls with recording URL'
    except Exception as e:
        results['audio_url'] = f'FAIL: {str(e)}'

    return jsonify(results)

@app.route('/')
def index():
    return send_from_directory('.', 'qa-dashboard.html')

try:
    init_db()
except Exception as e:
    print(f'⚠️ DB init warning: {e}')

if __name__ == '__main__':
    print('\n✅ VoiceGuard QA Server running!')
    app.run(debug=True, port=5000)
