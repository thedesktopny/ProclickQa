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

import re

def _rule_keywords(text):
    stopwords = {'the','a','an','to','of','in','on','at','before','after','must','should',
                 'agent','any','call','is','are','for','with','and','or','not','this','that'}
    words = re.findall(r'\b[a-z]{3,}\b', text.lower())
    return set(w for w in words if w not in stopwords)

def _rules_match(rule_text_a, rule_text_b, threshold=0.5):
    """True if two rule descriptions share enough keywords to be considered the same rule.
    Needed because Claude paraphrases/shortens rule text when scoring, so it rarely matches
    the original rule description verbatim."""
    kw_a = _rule_keywords(rule_text_a)
    kw_b = _rule_keywords(rule_text_b)
    if not kw_a or not kw_b:
        return False
    overlap = len(kw_a & kw_b)
    smaller = min(len(kw_a), len(kw_b))
    return (overlap / smaller) >= threshold if smaller else False

app = Flask(__name__, static_folder='.')
app.secret_key = os.getenv('SECRET_KEY', 'voiceguard-secret-2024-proclick-xK9mP2nQ')
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'vg_session'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 days
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

@app.before_request
def make_session_permanent():
    session.permanent = True
    if request.method == 'OPTIONS':
        return '', 204
CORS(app, resources={r"/api/*": {"origins": "*", "allow_headers": ["Content-Type", "Authorization", "X-Auth-Token"], "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}})

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
            assigned_qa_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # API usage tracking
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_usage (
            id SERIAL PRIMARY KEY,
            service TEXT NOT NULL,
            call_id TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            audio_seconds INTEGER DEFAULT 0,
            cost_usd NUMERIC(10,6) DEFAULT 0,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Manual cost entries (Azure, etc.)
    c.execute('''
        CREATE TABLE IF NOT EXISTS manual_costs (
            id SERIAL PRIMARY KEY,
            service TEXT NOT NULL,
            description TEXT,
            cost_usd NUMERIC(10,2) NOT NULL,
            billing_month TEXT NOT NULL,
            entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            role TEXT NOT NULL DEFAULT 'admin',
            username TEXT,
            full_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            role TEXT NOT NULL DEFAULT 'qa_user',
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')

    # Resolution log — when a QA user handles a flagged call
    c.execute('''
        CREATE TABLE IF NOT EXISTS resolutions (
            id SERIAL PRIMARY KEY,
            call_id TEXT NOT NULL,
            qa_user_id INTEGER NOT NULL,
            actions_taken TEXT NOT NULL,
            ai_resolution_score INTEGER,
            ai_resolution_feedback TEXT,
            resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS flagged_moments TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS agents_name_unique ON agents (name)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS assigned_qa_user_id INTEGER",
        """CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            role TEXT NOT NULL DEFAULT 'admin',
            username TEXT,
            full_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )""",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            pass

    # Seed admin user into users table (from env vars)
    try:
        admin_user = os.getenv('ADMIN_USERNAME', 'david')
        admin_pass_hash = hashlib.sha256(os.getenv('ADMIN_PASSWORD', 'admin123').encode()).hexdigest()
        c.execute('SELECT id FROM users WHERE username = %s', (admin_user,))
        if not c.fetchone():
            c.execute('''INSERT INTO users (username, password_hash, full_name, role)
                         VALUES (%s, %s, %s, 'admin')''',
                      (admin_user, admin_pass_hash, 'Administrator'))
    except Exception as e:
        print(f'Admin seed skipped: {e}')
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

# ─── TOKEN-BASED AUTH (replaces Flask sessions) ───────────────────────────────
import secrets

# In-memory token store — survives within a process, backed by DB for persistence
_token_cache = {}

def create_token(user_data):
    token = secrets.token_urlsafe(32)
    _token_cache[token] = user_data
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO auth_tokens (token, user_id, role, username, full_name, expires_at)
                     VALUES (%s, %s, %s, %s, %s, NOW() + INTERVAL '30 days')''',
                  (token, user_data.get('id'), user_data.get('role','admin'),
                   user_data.get('username'), user_data.get('full_name')))
        conn.commit()
        conn.close()
        print(f"[Auth] Token created for {user_data.get('username')} and saved to DB")
    except Exception as e:
        print(f'[Auth] Token DB save failed (will use memory cache): {e}')
    return token

def get_token_user(token):
    if not token:
        return None
    # Check cache first
    if token in _token_cache:
        return _token_cache[token]
    # Fall back to DB
    try:
        conn = get_db()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('SELECT * FROM auth_tokens WHERE token=%s AND expires_at > NOW()', (token,))
        row = c.fetchone()
        conn.close()
        if row:
            user_data = {'id': row['user_id'], 'role': row['role'],
                        'username': row['username'], 'full_name': row['full_name']}
            _token_cache[token] = user_data
            return user_data
    except Exception as e:
        print(f'Token lookup warning: {e}')
    return None

def get_request_token():
    # Check Authorization header first, then X-Auth-Token
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.headers.get('X-Auth-Token', '')

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_token_user(get_request_token())
        # Also check legacy Flask session
        if not user and (session.get('role') == 'admin' or session.get('admin')):
            user = {'role': 'admin', 'id': None, 'username': session.get('username'), 'full_name': session.get('full_name')}
        if not user or user.get('role') != 'admin':
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def require_login(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_token_user(get_request_token())
        if not user and (session.get('user_id') or session.get('admin')):
            user = {'role': session.get('role','admin'), 'id': session.get('user_id'),
                    'username': session.get('username'), 'full_name': session.get('full_name')}
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def current_user():
    user = get_token_user(get_request_token())
    if not user and (session.get('user_id') or session.get('admin')):
        user = {'role': session.get('role','admin'), 'id': session.get('user_id'),
                'username': session.get('username'), 'full_name': session.get('full_name')}
    return user

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
    username = (data.get('username', '') or '').strip()
    password_hash = hashlib.sha256(data.get('password', '').encode()).hexdigest()

    user_data = None

    try:
        conn = get_db()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('SELECT * FROM users WHERE username = %s AND status = %s', (username, 'active'))
        user = c.fetchone()
        if user and user['password_hash'] == password_hash:
            user_data = {'id': user['id'], 'role': user['role'],
                        'username': user['username'], 'full_name': user['full_name'] or user['username']}
            c.execute('UPDATE users SET last_login = NOW() WHERE id = %s', (user['id'],))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f'Login error: {e}')

    if not user_data:
        if username == ADMIN_USERNAME and password_hash == ADMIN_PASSWORD:
            user_data = {'id': None, 'role': 'admin', 'username': username, 'full_name': 'Administrator'}

    if not user_data:
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    token = create_token(user_data)
    return jsonify({
        'success': True,
        'token': token,
        'role': user_data['role'],
        'full_name': user_data['full_name']
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    token = get_request_token()
    if token:
        _token_cache.pop(token, None)
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('DELETE FROM auth_tokens WHERE token=%s', (token,))
            conn.commit()
            conn.close()
        except: pass
    session.clear()
    return jsonify({'success': True})

@app.route('/api/auth-check', methods=['GET'])
def auth_check():
    user = current_user()
    if user:
        return jsonify({'authenticated': True, 'role': user.get('role','admin'),
                       'username': user.get('username'), 'full_name': user.get('full_name')})
    return jsonify({'authenticated': False})

# ─── RULES ────────────────────────────────────────────────────────────────────
@app.route('/api/rules/<int:rule_id>/violations', methods=['GET'])
def get_rule_violations(rule_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # Get rule info
    c.execute('SELECT * FROM rules WHERE id=%s', (rule_id,))
    rule = c.fetchone()
    if not rule:
        conn.close()
        return jsonify({'error': 'Rule not found'}), 404

    rule = dict(rule)
    rule_desc = rule['description']

    # Find calls where this rule was violated from rule_results table — fetch then fuzzy match in Python
    # since Claude paraphrases rule text and exact substring matching misses most real matches
    c.execute('''
        SELECT rr.call_id, rr.rule_description, rr.evidence, rr.confidence,
               ca.agent_name, ca.account_name, ca.created_at,
               ca.overall_score, ca.status, ca.emotion
        FROM rule_results rr
        JOIN calls ca ON ca.call_id = rr.call_id
        WHERE rr.passed = false
        ORDER BY ca.created_at DESC
        LIMIT 2000
    ''')
    all_failed_results = c.fetchall()
    violations = []
    for row in all_failed_results:
        if row['rule_description'] and _rules_match(rule_desc, row['rule_description']):
            violations.append(dict(row))

    # Also search scorecard JSON for calls without rule_results entries
    if len(violations) < 5:
        c.execute('''
            SELECT call_id, agent_name, account_name, created_at,
                   overall_score, status, emotion, scorecard
            FROM calls
            WHERE overall_score > 0
            AND scorecard IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 300
        ''')
        for row in c.fetchall():
            if any(v['call_id'] == row['call_id'] for v in violations):
                continue
            try:
                sc = json.loads(row['scorecard']) if isinstance(row['scorecard'], str) else row['scorecard']
                for rule_eval in sc.get('rules_evaluation', []):
                    if not rule_eval.get('passed') and _rules_match(rule_desc, rule_eval.get('rule','')):
                        violations.append({
                            'call_id': row['call_id'],
                            'agent_name': row['agent_name'],
                            'account_name': row['account_name'],
                            'created_at': row['created_at'],
                            'overall_score': row['overall_score'],
                            'status': row['status'],
                            'emotion': row['emotion'],
                            'evidence': rule_eval.get('evidence','')
                        })
                        break
            except: pass

    # Aggregate by agent
    agent_counts = {}
    for v in violations:
        name = v['agent_name'] or 'Unknown'
        if name not in agent_counts:
            agent_counts[name] = {'count': 0, 'calls': []}
        agent_counts[name]['count'] += 1
        if len(agent_counts[name]['calls']) < 5:
            agent_counts[name]['calls'].append(v)

    agents_summary = sorted(
        [{'agent': k, 'count': v['count'], 'calls': v['calls']} for k, v in agent_counts.items()],
        key=lambda x: x['count'], reverse=True
    )

    conn.close()
    return jsonify({
        'rule': rule,
        'total_violations': len(violations),
        'agents_summary': agents_summary,
        'recent_violations': violations[:20]
    })

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

    old_severity = rule['severity']
    new_severity = data.get('severity', rule['severity'])
    new_description = data.get('description', rule['description'])

    c.execute('UPDATE rules SET description=%s, category=%s, severity=%s, active=%s WHERE id=%s RETURNING *',
              (new_description, data.get('category', rule['category']),
               new_severity, data.get('active', rule['active']), rule_id))
    updated = dict(c.fetchone())
    conn.commit()

    relabeled_count = 0
    if new_severity != old_severity:
        relabeled_count = relabel_rule_severity_in_past_calls(c, conn, rule['description'], new_severity)

    conn.close()
    updated['relabeled_calls'] = relabeled_count
    return jsonify(updated)

def relabel_rule_severity_in_past_calls(c, conn, rule_description, new_severity):
    """
    Updates severity of this rule across all past calls' rule_results and scorecard JSON,
    then recalculates flag counts and status for any affected call.
    Does NOT re-run AI scoring — overall_score is left untouched.
    Uses fuzzy keyword matching since Claude paraphrases rule text when scoring.
    """
    rule_keywords = _rule_keywords(rule_description)
    if not rule_keywords:
        return 0

    # 1. Update rule_results table — fetch all distinct rule_description values once, match in Python
    c.execute('SELECT DISTINCT rule_description, call_id FROM rule_results WHERE passed IS NOT NULL')
    all_results = c.fetchall()
    matching_call_ids_from_results = set()
    matching_descriptions = set()
    for row in all_results:
        if row['rule_description'] and _rules_match(rule_description, row['rule_description']):
            matching_descriptions.add(row['rule_description'])
            matching_call_ids_from_results.add(row['call_id'])

    if matching_descriptions:
        for desc in matching_descriptions:
            c.execute('UPDATE rule_results SET severity=%s WHERE rule_description=%s', (new_severity, desc))

    # 2. Find calls whose scorecard JSON mentions a similar rule (covers calls without rule_results rows)
    c.execute('''
        SELECT call_id, scorecard FROM calls
        WHERE scorecard IS NOT NULL AND scorecard != '{}'
        AND overall_score > 0
    ''')
    scorecard_rows = c.fetchall()

    affected_calls = set(matching_call_ids_from_results)

    for row in scorecard_rows:
        call_id = row['call_id']
        try:
            sc = json.loads(row['scorecard']) if isinstance(row['scorecard'], str) else row['scorecard']
            changed = False
            matched_rule_texts = set()

            for rule_eval in sc.get('rules_evaluation', []):
                rtext = rule_eval.get('rule', '')
                if rtext and _rules_match(rule_description, rtext):
                    rule_eval['severity'] = new_severity
                    matched_rule_texts.add(rtext.lower())
                    changed = True

            if matched_rule_texts:
                for flag in sc.get('flags', []):
                    flag_text = (flag.get('title','') + ' ' + flag.get('description','')).lower()
                    if any(_rules_match(rtext, flag_text, threshold=0.3) for rtext in matched_rule_texts):
                        flag['severity'] = new_severity
                        changed = True

            if changed:
                new_critical_count = sum(1 for f in sc.get('flags', []) if f.get('severity') == 'Critical')
                new_warning_count = sum(1 for f in sc.get('flags', []) if f.get('severity') == 'Warning')
                total_flags = len(sc.get('flags', []))

                if new_critical_count > 0:
                    new_status = 'Critical'
                elif new_warning_count > 0 or total_flags > 0:
                    new_status = 'Review'
                else:
                    new_status = 'Passed'

                c.execute('UPDATE calls SET scorecard=%s, flags=%s, status=%s WHERE call_id=%s',
                          (json.dumps(sc), total_flags, new_status, call_id))
                affected_calls.add(call_id)
        except Exception as e:
            print(f'[Relabel] Skipped call {call_id}: {e}')

    conn.commit()
    return len(affected_calls)

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

# ─── COSTS ────────────────────────────────────────────────────────────────────
@app.route('/api/costs', methods=['GET'])
@require_admin
def get_costs():
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # API usage by service for this month
    c.execute('''
        SELECT
            service,
            COUNT(*) as api_calls,
            SUM(input_tokens) as total_input_tokens,
            SUM(output_tokens) as total_output_tokens,
            SUM(audio_seconds) as total_audio_seconds,
            SUM(cost_usd) as total_cost,
            DATE(used_at) as day
        FROM api_usage
        WHERE TO_CHAR(used_at, 'YYYY-MM') = %s
        GROUP BY service, DATE(used_at)
        ORDER BY day ASC
    ''', (month,))
    usage_by_day = [dict(r) for r in c.fetchall()]

    # Totals by service
    c.execute('''
        SELECT
            service,
            COUNT(*) as api_calls,
            SUM(input_tokens) as total_input_tokens,
            SUM(output_tokens) as total_output_tokens,
            SUM(audio_seconds) as total_audio_seconds,
            ROUND(SUM(cost_usd)::numeric, 4) as total_cost
        FROM api_usage
        WHERE TO_CHAR(used_at, 'YYYY-MM') = %s
        GROUP BY service
    ''', (month,))
    usage_totals = {r['service']: dict(r) for r in c.fetchall()}

    # Manual costs for this month
    c.execute('''
        SELECT * FROM manual_costs
        WHERE billing_month = %s
        ORDER BY service ASC
    ''', (month,))
    manual = [dict(r) for r in c.fetchall()]

    # All-time totals
    c.execute('SELECT ROUND(SUM(cost_usd)::numeric, 4) as total FROM api_usage')
    alltime_api = c.fetchone()['total'] or 0

    c.execute('SELECT ROUND(SUM(cost_usd)::numeric, 2) as total FROM manual_costs')
    alltime_manual = c.fetchone()['total'] or 0

    # Monthly totals for chart (last 6 months)
    c.execute('''
        SELECT
            TO_CHAR(used_at, 'YYYY-MM') as month,
            ROUND(SUM(cost_usd)::numeric, 4) as api_cost
        FROM api_usage
        GROUP BY TO_CHAR(used_at, 'YYYY-MM')
        ORDER BY month DESC LIMIT 6
    ''')
    monthly = [dict(r) for r in c.fetchall()]

    conn.close()

    claude_total = float(usage_totals.get('claude', {}).get('total_cost', 0) or 0)
    gemini_total = float(usage_totals.get('gemini', {}).get('total_cost', 0) or 0)
    manual_total = sum(float(m['cost_usd']) for m in manual)
    month_total = claude_total + gemini_total + manual_total

    return jsonify({
        'month': month,
        'usage_totals': usage_totals,
        'usage_by_day': usage_by_day,
        'manual_costs': manual,
        'monthly_history': monthly,
        'summary': {
            'claude': round(claude_total, 4),
            'gemini': round(gemini_total, 4),
            'manual': round(manual_total, 2),
            'month_total': round(month_total, 2),
            'alltime_api': float(alltime_api),
            'alltime_total': round(float(alltime_api) + float(alltime_manual), 2)
        }
    })

@app.route('/api/costs/manual', methods=['POST'])
@require_admin
def add_manual_cost():
    data = request.json
    service = data.get('service', '').strip()
    description = data.get('description', '').strip()
    cost_usd = float(data.get('cost_usd', 0))
    billing_month = data.get('billing_month', datetime.now().strftime('%Y-%m'))
    if not service or cost_usd <= 0:
        return jsonify({'error': 'Service and cost required'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''INSERT INTO manual_costs (service, description, cost_usd, billing_month)
                 VALUES (%s, %s, %s, %s) RETURNING *''',
              (service, description, cost_usd, billing_month))
    row = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(row), 201

@app.route('/api/costs/manual/<int:cost_id>', methods=['DELETE'])
@require_admin
def delete_manual_cost(cost_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM manual_costs WHERE id=%s', (cost_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})
@app.route('/api/qa-users', methods=['GET'])
@require_admin
def get_qa_users():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT u.*,
            COUNT(DISTINCT a.id) as assigned_agents,
            COUNT(DISTINCT c.id) as total_calls_covered,
            AVG(c.overall_score) as avg_score_covered,
            COUNT(DISTINCT r.id) as resolutions_done,
            AVG(r.ai_resolution_score) as avg_resolution_score
        FROM users u
        LEFT JOIN agents a ON a.assigned_qa_user_id = u.id
        LEFT JOIN calls c ON c.agent_name = a.name AND c.status IN ('Review','Critical','Passed')
        LEFT JOIN resolutions r ON r.qa_user_id = u.id
        WHERE u.role = 'qa_user'
        GROUP BY u.id
        ORDER BY u.full_name ASC
    ''')
    users = [dict(u) for u in c.fetchall()]
    conn.close()
    # Remove password hash from response
    for u in users:
        u.pop('password_hash', None)
    return jsonify(users)

@app.route('/api/qa-users', methods=['POST'])
@require_admin
def create_qa_user():
    data = request.json
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    full_name = (data.get('full_name') or '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = get_db()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''INSERT INTO users (username, password_hash, full_name, role)
                     VALUES (%s, %s, %s, 'qa_user') RETURNING id, username, full_name, role, status, created_at''',
                  (username, password_hash, full_name))
        user = dict(c.fetchone())
        conn.commit()
        conn.close()
        return jsonify(user), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/qa-users/<int:user_id>/drilldown', methods=['GET'])
@require_admin
def qa_user_drilldown(user_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # User info
    c.execute('SELECT * FROM users WHERE id=%s', (user_id,))
    user = dict(c.fetchone() or {})
    user.pop('password_hash', None)

    # Assigned agents with their stats
    c.execute('''
        SELECT a.*,
            COUNT(c.id) as total_calls,
            COUNT(CASE WHEN c.overall_score > 0 THEN 1 END) as scored_calls,
            COUNT(CASE WHEN c.status IN ('Review','Critical') AND c.overall_score > 0 THEN 1 END) as flagged_calls,
            ROUND(AVG(CASE WHEN c.overall_score > 0 THEN c.overall_score END)) as avg_score
        FROM agents a
        LEFT JOIN calls c ON c.agent_name = a.name
        WHERE a.assigned_qa_user_id = %s
        GROUP BY a.id ORDER BY a.name ASC
    ''', (user_id,))
    agents = [dict(a) for a in c.fetchall()]

    # Recent resolutions with call info
    c.execute('''
        SELECT r.*, ca.agent_name, ca.account_name, ca.overall_score, ca.status as call_status
        FROM resolutions r
        LEFT JOIN calls ca ON ca.call_id = r.call_id
        WHERE r.qa_user_id = %s
        ORDER BY r.resolved_at DESC LIMIT 20
    ''', (user_id,))
    resolutions = [dict(r) for r in c.fetchall()]

    # Unresolved flagged calls (Review/Critical with no resolution)
    c.execute('''
        SELECT c.call_id, c.agent_name, c.account_name, c.overall_score,
               c.status, c.flags, c.created_at, c.summary, c.human_review_reason
        FROM calls c
        JOIN agents a ON a.name = c.agent_name
        LEFT JOIN resolutions r ON r.call_id = c.call_id AND r.qa_user_id = %s
        WHERE a.assigned_qa_user_id = %s
        AND c.status IN ('Review','Critical')
        AND c.overall_score > 0
        AND r.id IS NULL
        ORDER BY c.status DESC, c.overall_score ASC
        LIMIT 30
    ''', (user_id, user_id))
    unresolved = [dict(u) for u in c.fetchall()]

    # Performance summary
    total_flagged = sum(a.get('flagged_calls',0) or 0 for a in agents)
    total_resolved = len(resolutions)
    avg_res_score = round(sum(r.get('ai_resolution_score',0) or 0 for r in resolutions) / max(len(resolutions),1))
    coverage = round((total_resolved / max(total_flagged,1)) * 100)

    conn.close()
    return jsonify({
        'user': user,
        'agents': agents,
        'resolutions': resolutions,
        'unresolved': unresolved,
        'summary': {
            'assigned_agents': len(agents),
            'total_calls_covered': sum(a.get('total_calls',0) or 0 for a in agents),
            'total_flagged': total_flagged,
            'total_resolved': total_resolved,
            'coverage_pct': coverage,
            'avg_resolution_score': avg_res_score,
            'unresolved_count': len(unresolved)
        }
    })

@app.route('/api/qa-users/<int:user_id>', methods=['PUT'])
@require_admin
def update_qa_user(user_id):
    data = request.json
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM users WHERE id=%s AND role=%s', (user_id, 'qa_user'))
    user = c.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    updates = {}
    if data.get('full_name'): updates['full_name'] = data['full_name']
    if data.get('status'): updates['status'] = data['status']
    if data.get('password'):
        updates['password_hash'] = hashlib.sha256(data['password'].encode()).hexdigest()
    if updates:
        set_clause = ', '.join(f'{k}=%s' for k in updates)
        c.execute(f'UPDATE users SET {set_clause} WHERE id=%s', (*updates.values(), user_id))
        conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/qa-users/<int:user_id>', methods=['DELETE'])
@require_admin
def delete_qa_user(user_id):
    conn = get_db()
    c = conn.cursor()
    # Unassign all agents from this user first
    c.execute('UPDATE agents SET assigned_qa_user_id=NULL WHERE assigned_qa_user_id=%s', (user_id,))
    c.execute('DELETE FROM users WHERE id=%s AND role=%s', (user_id, 'qa_user'))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/qa-users/<int:user_id>/assign', methods=['POST'])
@require_admin
def assign_agents_to_qa_user(user_id):
    """Assign call agents to a QA user. Replaces existing assignments."""
    data = request.json
    agent_ids = data.get('agent_ids', [])
    conn = get_db()
    c = conn.cursor()
    # Remove this QA user from all agents first
    c.execute('UPDATE agents SET assigned_qa_user_id=NULL WHERE assigned_qa_user_id=%s', (user_id,))
    # Assign selected agents
    if agent_ids:
        c.execute(f'UPDATE agents SET assigned_qa_user_id=%s WHERE id = ANY(%s)', (user_id, agent_ids))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'assigned': len(agent_ids)})

@app.route('/api/qa-users/<int:user_id>/assignments', methods=['GET'])
@require_admin
def get_qa_user_assignments(user_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT a.*, COUNT(c.id) as total_calls, AVG(c.overall_score) as avg_score,
            COUNT(CASE WHEN c.status IN (\'Review\',\'Critical\') AND c.overall_score > 0 THEN 1 END) as needs_review
        FROM agents a
        LEFT JOIN calls c ON c.agent_name = a.name
        WHERE a.assigned_qa_user_id = %s
        GROUP BY a.id ORDER BY a.name ASC
    ''', (user_id,))
    agents = [dict(a) for a in c.fetchall()]
    conn.close()
    return jsonify(agents)

@app.route('/api/qa-users/performance', methods=['GET'])
@require_admin
def qa_user_performance():
    """Admin view: how each QA user is performing."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT
            u.id, u.username, u.full_name, u.last_login,
            COUNT(DISTINCT a.id) as assigned_agents,
            COUNT(DISTINCT CASE WHEN c.status IN ('Review','Critical') AND c.overall_score > 0 THEN c.id END) as flagged_calls,
            COUNT(DISTINCT r.call_id) as resolved_calls,
            ROUND(AVG(r.ai_resolution_score)) as avg_resolution_score,
            COUNT(DISTINCT CASE WHEN c.overall_score > 0 THEN c.id END) as total_scored_calls
        FROM users u
        LEFT JOIN agents a ON a.assigned_qa_user_id = u.id
        LEFT JOIN calls c ON c.agent_name = a.name
        LEFT JOIN resolutions r ON r.qa_user_id = u.id
        WHERE u.role = 'qa_user'
        GROUP BY u.id
        ORDER BY u.full_name ASC
    ''')
    perf = [dict(p) for p in c.fetchall()]
    conn.close()
    return jsonify(perf)

# ─── RESOLUTIONS ──────────────────────────────────────────────────────────────
@app.route('/api/resolutions', methods=['POST'])
@require_login
def submit_resolution():
    """QA user submits resolution for a flagged call."""
    data = request.json
    call_id = data.get('call_id')
    actions_taken = (data.get('actions_taken') or '').strip()
    user = current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    if not call_id or not actions_taken:
        return jsonify({'error': 'call_id and actions_taken required'}), 400

    # Get call details for AI scoring
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM calls WHERE call_id=%s', (call_id,))
    call = c.fetchone()
    if not call:
        conn.close()
        return jsonify({'error': 'Call not found'}), 404

    # AI scores the resolution
    ai_score = 0
    ai_feedback = ''
    try:
        import anthropic as anthropic_lib
        client = anthropic_lib.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
        sc = call.get('scorecard') or '{}'
        if isinstance(sc, str): sc = json.loads(sc)
        flags = sc.get('flags', [])
        flags_text = '\n'.join([f"- {f.get('title','')}: {f.get('description','')}" for f in flags]) or 'No flags'
        prompt = f"""You are evaluating a QA reviewer's response to a flagged call center call.

Call summary: {call.get('summary','N/A')}
Call score: {call.get('overall_score',0)}%
Flags identified:
{flags_text}

QA reviewer's actions taken:
{actions_taken}

Score the QA reviewer's response from 0-100 based on:
- Did they correctly understand what went wrong? (30 points)
- Were their actions appropriate and specific? (40 points)
- Was their response thorough and professional? (30 points)

Respond with ONLY valid JSON:
{{"score": 0-100, "feedback": "2-3 sentence assessment of what they did well and what could be better"}}"""
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = msg.content[0].text.strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            ai_score = result.get('score', 0)
            ai_feedback = result.get('feedback', '')
    except Exception as e:
        print(f'Resolution AI scoring failed: {e}')

    # Save resolution
    c.execute('''INSERT INTO resolutions (call_id, qa_user_id, actions_taken, ai_resolution_score, ai_resolution_feedback)
                 VALUES (%s, %s, %s, %s, %s) RETURNING *''',
              (call_id, user['id'], actions_taken, ai_score, ai_feedback))
    resolution = dict(c.fetchone())
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'ai_score': ai_score, 'ai_feedback': ai_feedback, 'resolution': resolution})

@app.route('/api/resolutions/<call_id>', methods=['GET'])
@require_login
def get_resolution(call_id):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''SELECT r.*, u.full_name as qa_user_name
                 FROM resolutions r JOIN users u ON r.qa_user_id = u.id
                 WHERE r.call_id = %s ORDER BY r.resolved_at DESC LIMIT 1''', (call_id,))
    res = c.fetchone()
    conn.close()
    return jsonify(dict(res) if res else {})

# ─── AGENTS ───────────────────────────────────────────────────────────────────
@app.route('/api/agent-profile/<path:agent_name>', methods=['GET'])
def agent_profile(agent_name):
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # Agent info
    c.execute('SELECT * FROM agents WHERE name=%s', (agent_name,))
    agent = dict(c.fetchone() or {'name': agent_name, 'extension': '—'})

    # All scored calls
    c.execute('''
        SELECT call_id, overall_score, status, emotion, flags, duration,
               billed_minutes, call_end_first, call_notes, notes_score,
               scorecard, summary, coaching_notes, created_at, account_name,
               requires_human_review, agent_qos_tx, agent_qos_rx
        FROM calls
        WHERE agent_name=%s AND overall_score > 0
        ORDER BY created_at DESC
        LIMIT 100
    ''', (agent_name,))
    calls = [dict(c) for c in c.fetchall()]

    # Category averages
    cat_totals = {}
    cat_counts = {}
    flag_counts = {}
    emotion_counts = {}
    notes_scores = []

    for call in calls:
        # Notes
        if call.get('notes_score') is not None:
            notes_scores.append(call['notes_score'])

        # Emotion
        emo = call.get('emotion') or ''
        if emo:
            emotion_counts[emo] = emotion_counts.get(emo, 0) + 1

        # Scorecard
        sc = call.get('scorecard') or '{}'
        if isinstance(sc, str):
            try: sc = json.loads(sc)
            except: sc = {}

        cat_scores = sc.get('category_scores', {})
        for cat, val in cat_scores.items():
            score = val.get('score', 0) if isinstance(val, dict) else 0
            cat_totals[cat] = cat_totals.get(cat, 0) + score
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        # Flags
        for flag in sc.get('flags', []):
            title = flag.get('title', 'Unknown')
            flag_counts[title] = flag_counts.get(title, 0) + 1

    # Build category averages
    cat_averages = {}
    for cat in cat_totals:
        cat_averages[cat] = round(cat_totals[cat] / cat_counts[cat])

    # Top flags
    top_flags = sorted(flag_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    # Overall stats
    scored = [c for c in calls if c['overall_score'] > 0]
    avg_score = round(sum(c['overall_score'] for c in scored) / max(len(scored), 1))
    passed = sum(1 for c in scored if c['status'] == 'Passed')
    review = sum(1 for c in scored if c['status'] == 'Review')
    critical = sum(1 for c in scored if c['status'] == 'Critical')
    avg_notes = round(sum(notes_scores) / max(len(notes_scores), 1)) if notes_scores else 0
    total_flags = sum(c.get('flags', 0) or 0 for c in scored)
    dropped = sum(1 for c in calls if c.get('call_end_first') == 'drop')
    agent_ended = sum(1 for c in calls if c.get('call_end_first') == 'agent')

    # Recent trend (last 10 calls avg vs previous 10)
    recent = scored[:10]
    older = scored[10:20]
    recent_avg = round(sum(c['overall_score'] for c in recent) / max(len(recent), 1)) if recent else 0
    older_avg = round(sum(c['overall_score'] for c in older) / max(len(older), 1)) if older else 0
    trend = 'improving' if recent_avg > older_avg + 3 else 'declining' if recent_avg < older_avg - 3 else 'stable'

    # Total calls including unscored
    c.execute('SELECT COUNT(*) as total FROM calls WHERE agent_name=%s', (agent_name,))
    total_calls = c.fetchone()['total']

    conn.close()

    return jsonify({
        'agent': agent,
        'stats': {
            'total_calls': total_calls,
            'scored_calls': len(scored),
            'avg_score': avg_score,
            'passed': passed,
            'review': review,
            'critical': critical,
            'avg_notes': avg_notes,
            'total_flags': total_flags,
            'dropped_calls': dropped,
            'agent_ended_calls': agent_ended,
            'recent_avg': recent_avg,
            'older_avg': older_avg,
            'trend': trend
        },
        'category_averages': cat_averages,
        'top_flags': top_flags,
        'emotion_distribution': emotion_counts,
        'recent_calls': calls[:20]
    })

@app.route('/api/agents', methods=['GET'])
def get_agents():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    user = current_user()
    if user and user['role'] == 'qa_user':
        c.execute('''
            SELECT a.*, COUNT(c.id) as total_calls, AVG(c.overall_score) as avg_score
            FROM agents a
            LEFT JOIN calls c ON c.agent_name = a.name
            WHERE a.assigned_qa_user_id = %s
            GROUP BY a.id ORDER BY a.name ASC
        ''', (user['id'],))
    else:
        c.execute('''
            SELECT a.*, COUNT(c.id) as total_calls, AVG(c.overall_score) as avg_score,
                u.full_name as qa_user_name, u.username as qa_user_username
            FROM agents a
            LEFT JOIN calls c ON c.agent_name = a.name
            LEFT JOIN users u ON u.id = a.assigned_qa_user_id
            GROUP BY a.id, u.id ORDER BY a.name ASC
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
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 25))
    offset = (page - 1) * limit
    user = current_user()

    if user and user['role'] == 'qa_user':
        c.execute('SELECT COUNT(*) FROM calls JOIN agents ON agents.name = calls.agent_name WHERE agents.assigned_qa_user_id = %s', (user['id'],))
        total = c.fetchone()['count']
        c.execute('''
            SELECT calls.call_id, calls.agent_name, calls.account_name, calls.customer_account_id,
                   calls.caller_id, calls.created_at, calls.duration, calls.billed_minutes,
                   calls.overall_score, calls.status, calls.emotion, calls.flags,
                   calls.call_end_first, calls.call_notes, calls.notes_score,
                   calls.requires_human_review, calls.agent_qos_tx, calls.agent_qos_rx,
                   calls.customer_qos_tx, calls.customer_qos_rx, calls.recording_url
            FROM calls
            JOIN agents ON agents.name = calls.agent_name
            WHERE agents.assigned_qa_user_id = %s
            ORDER BY calls.created_at DESC LIMIT %s OFFSET %s
        ''', (user['id'], limit, offset))
    else:
        c.execute('SELECT COUNT(*) FROM calls')
        total = c.fetchone()['count']
        c.execute('''
            SELECT call_id, agent_name, account_name, customer_account_id, caller_id,
                   created_at, duration, billed_minutes, overall_score, status, emotion,
                   flags, call_end_first, call_notes, notes_score, requires_human_review,
                   agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx, recording_url
            FROM calls
            ORDER BY created_at DESC LIMIT %s OFFSET %s
        ''', (limit, offset))
    calls = [dict(c) for c in c.fetchall()]
    conn.close()
    return jsonify({
        'calls': calls,
        'total': total,
        'page': page,
        'limit': limit,
        'pages': max(1, -(-total // limit))  # ceiling division
    })

@app.route('/api/calls/<call_id>/recording', methods=['GET'])
def proxy_recording(call_id):
    """Proxy the recording file so browser can download it."""
    import urllib.request
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT recording_url, agent_name, account_name FROM calls WHERE call_id=%s', (call_id,))
    call = c.fetchone()
    conn.close()
    if not call or not call['recording_url']:
        return jsonify({'error': 'Recording not found'}), 404
    url = call['recording_url'].strip().rstrip(':').rstrip('/')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VoiceGuard/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        agent = (call['agent_name'] or 'agent').replace(' ','_')
        filename = f"call_{call_id[-8:]}_{agent}.wav"
        from flask import Response
        return Response(
            data,
            mimetype='audio/wav',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': len(data)
            }
        )
    except Exception as e:
        return jsonify({'error': f'Could not download recording: {str(e)}'}), 500

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
    c.execute("SELECT COUNT(*) as v FROM calls WHERE status='Critical' AND overall_score > 0")
    critical_flags = c.fetchone()['v']
    c.execute("SELECT COUNT(DISTINCT agent_name) as v FROM calls WHERE overall_score < 70 AND overall_score > 0")
    needs_coaching = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM rules WHERE active=1")
    active_rules = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM agents WHERE status='active'")
    active_agents = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM calls WHERE (requires_human_review=true OR status='Critical') AND overall_score > 0")
    needs_review = c.fetchone()['v']
    c.execute("SELECT COUNT(*) as v FROM calls WHERE call_end_first='drop' AND callback_made=false AND status != 'Processing'")
    unresolved_drops = c.fetchone()['v']

    # Category averages from scorecard JSON
    cat_avgs = {}
    try:
        c.execute("""
            SELECT
                ROUND(AVG((scorecard::json->'category_scores'->'accuracy_and_information'->>'score')::numeric)) as accuracy,
                ROUND(AVG((scorecard::json->'category_scores'->'customer_service_quality'->>'score')::numeric)) as customer_service,
                ROUND(AVG((scorecard::json->'category_scores'->'active_listening'->>'score')::numeric)) as active_listening,
                ROUND(AVG((scorecard::json->'category_scores'->'compliance_and_handling'->>'score')::numeric)) as compliance,
                ROUND(AVG((scorecard::json->'category_scores'->'emotion_management'->>'score')::numeric)) as emotion_management,
                ROUND(AVG((scorecard::json->'category_scores'->'documentation_quality'->>'score')::numeric)) as documentation,
                ROUND(AVG((scorecard::json->'category_scores'->'script_and_language'->>'score')::numeric)) as script,
                ROUND(AVG((scorecard::json->'category_scores'->'call_closure'->>'score')::numeric)) as call_closure
            FROM calls WHERE overall_score > 0 AND scorecard IS NOT NULL AND scorecard != '{}'
        """)
        row = c.fetchone()
        if row:
            cat_avgs = {
                'accuracy_and_information': int(row['accuracy'] or 0),
                'customer_service_quality': int(row['customer_service'] or 0),
                'active_listening': int(row['active_listening'] or 0),
                'compliance_and_handling': int(row['compliance'] or 0),
                'emotion_management': int(row['emotion_management'] or 0),
                'documentation_quality': int(row['documentation'] or 0),
                'script_and_language': int(row['script'] or 0),
                'call_closure': int(row['call_closure'] or 0),
            }
    except Exception as e:
        print(f'Category avg error: {e}')

    conn.close()
    return jsonify({
        'total_calls': total_calls,
        'avg_score': round(float(avg_score), 1),
        'critical_flags': critical_flags,
        'needs_coaching': needs_coaching,
        'active_rules': active_rules,
        'active_agents': active_agents,
        'needs_human_review': needs_review,
        'unresolved_drops': unresolved_drops,
        'category_averages': cat_avgs
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

        # Always process in background thread — respond to Igor instantly
        import threading

        def process_new_call():
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

                conn2 = get_db()
                c2 = conn2.cursor()
                c2.execute('''
                    UPDATE calls SET duration=%s, overall_score=%s, confidence=%s,
                        emotion=%s, status=%s, flags=%s, scorecard=%s, transcript=%s,
                        summary=%s, emotion_delta=%s, requires_human_review=%s,
                        human_review_reason=%s, age_concern=%s, coaching_notes=%s,
                        positive_highlights=%s, call_dropped=%s, notes_score=%s,
                        notes_feedback=%s, flagged_moments=%s
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
                    json.dumps(result.get('flagged_moments', [])),
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
                print(f"[AutoProcess] ✅ {call_id} scored: {result['overall_score']}%")

            except Exception as e:
                print(f"[AutoProcess] ❌ {call_id} failed: {e}")
                try:
                    conn3 = get_db()
                    c3 = conn3.cursor()
                    c3.execute("UPDATE calls SET status='Failed' WHERE call_id=%s", (call_id,))
                    conn3.commit()
                    conn3.close()
                except: pass
            finally:
                try: os.remove(audio_path)
                except: pass

        thread = threading.Thread(target=process_new_call, daemon=True)
        thread.start()

        # Return immediately to Igor — don't make him wait
        return jsonify({
            'success': True, 'call_id': call_id, 'status': 'Processing',
            'message': 'Call received. Analysis running in background — results in 1-2 minutes.'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── RETRY STUCK CALLS ────────────────────────────────────────────────────────
# ─── RETRY STATUS LOG ─────────────────────────────────────────────────────────
retry_log = []
retry_running = False

@app.route('/api/retry-status', methods=['GET'])
def retry_status():
    return jsonify({
        'running': retry_running,
        'log': retry_log[-50:]  # Last 50 entries
    })

@app.route('/api/retry-stuck', methods=['POST'])
@require_admin
def retry_stuck_calls():
    global retry_log, retry_running
    import threading

    if retry_running:
        return jsonify({'message': 'Retry already running', 'log': retry_log[-10:]})

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT call_id, agent_name, agent_extension, recording_url,
               call_duration_seconds, billed_minutes, caller_id,
               customer_account_id, account_name, call_end_first,
               agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx,
               call_notes, call_dropped
        FROM calls
        WHERE status IN ('Processing', 'Failed')
        AND overall_score = 0
        AND created_at < NOW() - INTERVAL '5 minutes'
        ORDER BY call_duration_seconds ASC NULLS LAST
        LIMIT 20
    ''')
    stuck_calls = [dict(c) for c in c.fetchall()]
    conn.close()

    if not stuck_calls:
        retry_log = [{'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': '✅ No stuck calls found — all done!', 'type': 'success'}]
        return jsonify({'message': 'No stuck calls found', 'count': 0})

    retry_log = [{'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'🔄 Starting retry for {len(stuck_calls)} calls...', 'type': 'info'}]

    def process_batch():
        global retry_running
        retry_running = True
        from ai_engine import analyze_call as run_analysis
        import urllib.request

        for i, call in enumerate(stuck_calls):
            try:
                call_id = call['call_id']
                agent = (call['agent_name'] or 'Unknown').strip()
                customer = (call['account_name'] or call['customer_account_id'] or '').strip()
                recording_url = (call['recording_url'] or '').strip().rstrip(':').rstrip('/')

                if not recording_url:
                    retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'⏭️ Skipped #{call_id[-8:]} — no recording URL', 'type': 'warning'})
                    continue

                retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'🔄 [{i+1}/{len(stuck_calls)}] {agent} / {customer} — downloading audio...', 'type': 'info'})

                url_path = recording_url.split('?')[0]
                ext = os.path.splitext(url_path)[1].lower()
                if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
                    ext = '.wav'

                UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                audio_path = os.path.join(UPLOAD_DIR, f"retry_{call_id}_{int(time.time())}{ext}")

                try:
                    urllib.request.urlretrieve(recording_url, audio_path)
                    size_bytes = os.path.getsize(audio_path)
                    size_mb = round(size_bytes / 1024 / 1024, 1)

                    # Skip files over 25MB (very long calls) — process them last
                    if size_mb > 25:
                        retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'⏭️ [{i+1}/{len(stuck_calls)}] {agent} / {customer} — skipped ({size_mb}MB, too large for now)', 'type': 'warning'})
                        try: os.remove(audio_path)
                        except: pass
                        continue

                    retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'🤖 [{i+1}/{len(stuck_calls)}] {agent} / {customer} — analyzing ({size_mb}MB)...', 'type': 'info'})

                    result = run_analysis(
                        audio_path, agent, call_id,
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
                            notes_feedback=%s, flagged_moments=%s
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
                        json.dumps(result.get('flagged_moments', [])),
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

                    score = result['overall_score']
                    status = result['status']
                    emotion = result.get('emotion','')
                    icon = '✅' if status == 'Passed' else '⚠️' if status == 'Review' else '🚨'
                    retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'{icon} {agent} / {customer} — {score}% | {status} | {emotion}', 'type': status.lower()})

                finally:
                    try: os.remove(audio_path)
                    except: pass

                time.sleep(5)

            except Exception as e:
                call_id = call.get('call_id','?')
                agent = (call.get('agent_name') or '?').strip()
                retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'❌ {agent} #{call_id[-8:]} — {str(e)[:80]}', 'type': 'error'})
                try:
                    conn3 = get_db()
                    c3 = conn3.cursor()
                    c3.execute("UPDATE calls SET status='Failed' WHERE call_id=%s", (call_id,))
                    conn3.commit()
                    conn3.close()
                except: pass
                time.sleep(5)

        retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'✅ Batch complete — processed {len(stuck_calls)} calls', 'type': 'success'})
        retry_running = False

    thread = threading.Thread(target=process_batch, daemon=True)
    thread.start()

    return jsonify({
        'message': f'Retry started for {len(stuck_calls)} calls',
        'count': len(stuck_calls),
        'call_ids': [c['call_id'] for c in stuck_calls]
    })

@app.route('/api/auth-debug', methods=['GET'])
def auth_debug():
    """Debug endpoint to check token status."""
    token = get_request_token()
    cache_hit = token in _token_cache if token else False
    db_result = None
    try:
        if token:
            conn = get_db()
            c = conn.cursor(cursor_factory=RealDictCursor)
            c.execute('SELECT username, role, expires_at FROM auth_tokens WHERE token=%s', (token,))
            row = c.fetchone()
            db_result = dict(row) if row else 'not found in DB'
            conn.close()
    except Exception as e:
        db_result = f'DB error: {str(e)}'
    return jsonify({
        'token_present': bool(token),
        'token_prefix': token[:8] + '...' if token else None,
        'cache_hit': cache_hit,
        'db_result': db_result,
        'cache_size': len(_token_cache)
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

@app.route('/api/test-one-call', methods=['GET'])
def test_one_call():
    """Process one stuck call and return detailed result."""
    import urllib.request
    import traceback

    try:
        conn = get_db()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT call_id, agent_name, recording_url, call_notes, account_name,
                   call_end_first, agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx
            FROM calls
            WHERE status IN ('Processing', 'Failed')
            AND overall_score = 0
            AND recording_url IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ''')
        call = c.fetchone()
        conn.close()

        if not call:
            return jsonify({'error': 'No stuck calls found'})

        call = dict(call)
        call_id = call['call_id']
        recording_url = (call['recording_url'] or '').strip().rstrip(':').rstrip('/')

        results = {'call_id': call_id, 'recording_url': recording_url, 'steps': {}}

        # Step 1: Download audio
        url_path = recording_url.split('?')[0]
        ext = os.path.splitext(url_path)[1].lower()
        if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
            ext = '.wav'

        UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        audio_path = os.path.join(UPLOAD_DIR, f"test_{call_id}{ext}")

        try:
            urllib.request.urlretrieve(recording_url, audio_path)
            size = os.path.getsize(audio_path)
            results['steps']['download'] = f'OK — {size} bytes ({round(size/1024/1024,1)} MB)'
        except Exception as e:
            results['steps']['download'] = f'FAIL: {str(e)}'
            return jsonify(results)

        # Step 2: Run AI
        try:
            from ai_engine import analyze_call as run_analysis
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
            )
            results['steps']['ai_analysis'] = f"OK — Score: {result['overall_score']}% | Status: {result['status']}"
            results['score'] = result['overall_score']
            results['emotion'] = result['emotion']
            results['summary'] = result.get('summary', '')[:200]

            # Save result
            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute('''UPDATE calls SET duration=%s, overall_score=%s, confidence=%s,
                emotion=%s, status=%s, flags=%s, scorecard=%s, transcript=%s, summary=%s,
                emotion_delta=%s, requires_human_review=%s, human_review_reason=%s,
                age_concern=%s, coaching_notes=%s, positive_highlights=%s,
                call_dropped=%s, notes_score=%s, notes_feedback=%s, flagged_moments=%s WHERE call_id=%s''',
                (result.get('duration','--'), result['overall_score'], result.get('confidence',100),
                 result['emotion'], result['status'], result['flags'],
                 json.dumps(result.get('scorecard',{})), result.get('transcript',''),
                 result.get('summary',''), json.dumps(result.get('emotion_delta',{})),
                 result.get('requires_human_review',False), result.get('human_review_reason',''),
                 json.dumps(result.get('age_concern',{})), result.get('coaching_notes',''),
                 result.get('positive_highlights',''), result.get('call_dropped',False),
                 result.get('notes_score',0), result.get('notes_feedback',''),
                 json.dumps(result.get('flagged_moments', [])), call_id))
            conn2.commit()
            conn2.close()
            results['steps']['saved'] = 'OK — saved to database'

        except Exception as e:
            results['steps']['ai_analysis'] = f'FAIL: {str(e)}'
            results['traceback'] = traceback.format_exc()
        finally:
            try: os.remove(audio_path)
            except: pass

        return jsonify(results)

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()})

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
            model='claude-sonnet-4-6',
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
