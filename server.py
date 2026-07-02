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
import urllib.request
import urllib.error
import urllib.parse

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

# Recording relay — Central US Azure cannot reach main.getremail.com directly (regional
# network block confirmed via diagnostics), so a small relay app in Canada Central
# (where the recording server IS reachable) fetches recordings on our behalf.
# If RELAY_URL is unset, falls back to direct download (useful for local testing or if
# the network block ever resolves on its own).
RELAY_URL = os.getenv('RELAY_URL', '')  # e.g. https://voiceguard-recording-relay.azurewebsites.net
RELAY_SECRET = os.getenv('RELAY_SECRET', '')

# Global pause switch — when True, incoming calls are saved but AI analysis is skipped.
# Toggle via /api/processing-status (admin only). Checked fresh on every request, not
# cached, so changes take effect immediately without needing a restart.
def is_processing_paused():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM app_settings WHERE key='processing_paused'")
    row = c.fetchone()
    conn.close()
    return row and row[0] == 'true'

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def download_recording(url, dest_path, timeout=60, retries=2):
    """
    Downloads a call recording with an explicit timeout and retry logic.
    Raises a descriptive exception on failure instead of hanging silently.
    Returns nothing on success — file is written to dest_path.

    If RELAY_URL is configured, routes the download through a relay app hosted
    in a different Azure region (Canada Central) that can actually reach the
    recording server, since our main region (Central US) cannot — confirmed via
    direct network diagnostics (DNS works, but TCP connection times out on both
    port 80 and 443, specifically to that one host).
    """
    import socket

    if RELAY_URL:
        fetch_url = f"{RELAY_URL.rstrip('/')}/fetch?url={urllib.parse.quote(url, safe='')}"
        headers = {'X-Relay-Secret': RELAY_SECRET}
    else:
        fetch_url = url
        headers = {'User-Agent': 'VoiceGuard/1.0'}

    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(fetch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if len(data) < 1000:
                # Relay may have returned a JSON error body instead of audio — surface it
                try:
                    err_json = json.loads(data)
                    raise ValueError(f"Relay/server error: {err_json.get('error', data[:200])}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ValueError(f'Downloaded file suspiciously small ({len(data)} bytes) — likely an error page, not real audio')
            with open(dest_path, 'wb') as f:
                f.write(data)
            return  # success
        except socket.timeout as e:
            last_error = f'Timed out after {timeout}s connecting to {"relay" if RELAY_URL else "recording server"} (attempt {attempt+1}/{retries+1})'
        except urllib.error.HTTPError as e:
            last_error = f'{"Relay" if RELAY_URL else "Recording server"} returned HTTP {e.code} (attempt {attempt+1}/{retries+1})'
        except urllib.error.URLError as e:
            last_error = f'Could not reach {"relay" if RELAY_URL else "recording server"}: {e.reason} (attempt {attempt+1}/{retries+1})'
        except Exception as e:
            last_error = f'{type(e).__name__}: {str(e)} (attempt {attempt+1}/{retries+1})'

        if attempt < retries:
            time.sleep(3)  # brief pause before retry

    raise Exception(f'Recording download failed after {retries+1} attempts. Last error: {last_error}. URL: {url}')

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
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS flag_reviews (
            id SERIAL PRIMARY KEY,
            call_id TEXT,
            flag_index INTEGER,
            flag_title TEXT,
            flag_rule TEXT,
            resolution_note TEXT,
            marked_ai_mistake BOOLEAN DEFAULT FALSE,
            reviewed_by INTEGER,
            reviewer_name TEXT,
            manager_status TEXT DEFAULT 'none',
            manager_id INTEGER,
            manager_note TEXT,
            resolution_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS learned_exceptions (
            id SERIAL PRIMARY KEY,
            rule_id INTEGER,
            rule_description TEXT,
            exception_text TEXT,
            source_call_id TEXT,
            approved_by INTEGER,
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS pipeline_comparisons (
            id SERIAL PRIMARY KEY,
            call_id TEXT,
            claude_gemini_scorecard TEXT,
            gemini_only_scorecard TEXT,
            claude_gemini_cost NUMERIC DEFAULT 0,
            gemini_only_cost NUMERIC DEFAULT 0,
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
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS error_message TEXT",
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
            # ── SIMPLE / SINGLE-CONDITION RULES ──
            ('Agent must never use inappropriate, offensive, or sexual language of any kind', 'Forbidden Words', 'Critical'),
            ('Agent must verify the customer identity at the start of every call before accessing any account', 'Compliance', 'Critical'),
            ('Agent must not discuss working outside of Proclick or solicit personal contact with customers', 'Conduct', 'Critical'),
            ('Agent should not overuse the word "sir" — using it too frequently sounds robotic', 'Behavior', 'Warning'),
            ('Agent must ask "Is there anything else I can help you with today?" before ending the call', 'Required Phrases', 'Info'),
            ('Agent must write detailed and accurate call notes after every call', 'Documentation', 'Warning'),
            ('Agent must not ask the customer to repeat information already provided in the same call — if the customer repeats themselves or the agent uses incorrect details that were already stated, this is an active listening failure', 'Active Listening', 'Warning'),
            ('Agent must follow the customer\'s exact instructions — if the customer says a link, item, or order already exists on their account or was saved from a previous call, the agent must retrieve it instead of starting the search from scratch', 'Instruction Following', 'Warning'),
            ('Agent must directly answer the customer\'s question — if a customer asks a specific question, the agent must answer it before moving on; partially answering, changing the subject, or ignoring the question entirely is a violation', 'Customer Service', 'Warning'),
            ('Agent must provide a verbal update to the customer if working in silence for more than 30 seconds — silence exceeding 30 seconds without any update or acknowledgment is a violation; flag should include how long the silence lasted', 'Dead Air', 'Warning'),
            ('Agent must ensure their environment is free from disruptive background noise during the call — background conversations, TV, fan noise, crying, animals, or poor microphone quality that interferes with the call is a violation', 'Audio Quality', 'Warning'),
            ('Agent must not reference the wrong country, region, currency, or website for the customer\'s context — if the customer is discussing UK services and the agent references US options, or the customer asks about Canadian pricing and the agent quotes USD, this is a mismatch violation', 'Region Mismatch', 'Warning'),

            # ── FRUSTRATION (split into 2) ──
            ('If a customer sounds frustrated or upset, agent must acknowledge their feelings before continuing', 'Customer Frustration', 'Warning'),
            ('When a customer is genuinely frustrated, agent must identify and address the root cause of the frustration, not just acknowledge it and move on', 'Customer Frustration', 'Warning'),

            # ── PROFESSIONALISM (split into 5) ──
            ('Agent must not interrupt the customer while they are speaking', 'Professionalism', 'Warning'),
            ('Agent must not raise their voice at the customer', 'Professionalism', 'Warning'),
            ('Agent must not use sarcasm with the customer', 'Professionalism', 'Warning'),
            ('Agent must not give dismissive responses to the customer', 'Professionalism', 'Warning'),
            ('Agent must not use clearly inappropriate or unprofessional language toward the customer', 'Professionalism', 'Warning'),

            # ── CALL DROP & CALLBACK (split into 2) ──
            ('If the agent ends or drops the call mid-conversation while the customer\'s issue is unresolved, the agent must call back within 5 minutes', 'Call Drop', 'Critical'),
            ('If the call drops from the customer\'s side mid-conversation while their issue is unresolved, the agent must attempt a callback within 5 minutes', 'Call Drop', 'Warning'),

            # ── RESTRICTED CONTENT / AGE (split into 3) ──
            ('Caller states they are under 18, or clearly appears to be a minor based on voice and context', 'Restricted Content', 'Critical'),
            ('A caller who appears to be or states they are under 18 requests a smartphone or account access requiring adult authorization', 'Restricted Content', 'Critical'),
            ('Customer requests sexual, explicit, or adult-content-related products or services at any point in the call', 'Restricted Content', 'Critical'),

            # ── BILLING COMPLIANCE (split into 4) ──
            ('Agent overcharged the customer relative to actual call activity', 'Billing Compliance', 'Warning'),
            ('Agent undercharged the customer relative to actual call activity', 'Billing Compliance', 'Warning'),
            ('Agent continued providing assistance after the customer\'s minutes were exhausted without addressing it', 'Billing Compliance', 'Warning'),
            ('Agent failed to offer a refill or top-up when the customer\'s minutes ran out', 'Billing Compliance', 'Warning'),
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

def require_manager(f):
    """Allows users with role 'manager' OR 'admin' (admin outranks manager)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_token_user(get_request_token())
        if not user and (session.get('role') in ('admin','manager') or session.get('admin')):
            user = {'role': session.get('role','admin'), 'id': session.get('user_id'),
                    'username': session.get('username'), 'full_name': session.get('full_name')}
        if not user or user.get('role') not in ('manager', 'admin'):
            return jsonify({'error': 'Manager or admin access required'}), 401
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
@require_manager
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
@require_manager
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
@require_manager
def delete_rule(rule_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rules WHERE id=%s', (rule_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/rules/<int:rule_id>/toggle', methods=['POST'])
@require_manager
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
@require_manager
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
        WHERE u.role IN ('qa_user', 'manager')
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
@require_manager
def create_qa_user():
    acting_user = current_user()
    acting_role = acting_user.get('role') if acting_user else 'admin'
    data = request.json
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    full_name = (data.get('full_name') or '').strip()
    role = data.get('role', 'qa_user')
    if role not in ('qa_user', 'manager'):
        role = 'qa_user'
    # Only admins can create managers. A manager can only create qa_users.
    if role == 'manager' and acting_role != 'admin':
        return jsonify({'error': 'Only an admin can create manager accounts.'}), 403
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = get_db()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''INSERT INTO users (username, password_hash, full_name, role)
                     VALUES (%s, %s, %s, %s) RETURNING id, username, full_name, role, status, created_at''',
                  (username, password_hash, full_name, role))
        user = dict(c.fetchone())
        conn.commit()
        conn.close()
        return jsonify(user), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/qa-users/<int:user_id>/drilldown', methods=['GET'])
@require_manager
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
@require_manager
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
@require_manager
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
@require_manager
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
@require_manager
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
@require_manager
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
        WHERE u.role IN ('qa_user', 'manager')
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

    date_from = request.args.get('date_from', '')  # ISO format: 2026-06-17T00:00:00
    date_to = request.args.get('date_to', '')

    date_clause = ''
    date_params = []
    if date_from:
        date_clause += ' AND calls.created_at >= %s'
        date_params.append(date_from)
    if date_to:
        date_clause += ' AND calls.created_at <= %s'
        date_params.append(date_to)

    if user and user['role'] == 'qa_user':
        c.execute(f'SELECT COUNT(*) FROM calls JOIN agents ON agents.name = calls.agent_name WHERE agents.assigned_qa_user_id = %s{date_clause}',
                  [user['id']] + date_params)
        total = c.fetchone()['count']
        c.execute(f'''
            SELECT calls.call_id, calls.agent_name, calls.account_name, calls.customer_account_id,
                   calls.caller_id, calls.created_at, calls.duration, calls.billed_minutes,
                   calls.call_duration_seconds, calls.overall_score, calls.status, calls.emotion,
                   calls.flags, calls.call_end_first, calls.call_notes, calls.notes_score,
                   calls.requires_human_review, calls.agent_qos_tx, calls.agent_qos_rx,
                   calls.customer_qos_tx, calls.customer_qos_rx, calls.recording_url,
                   calls.error_message
            FROM calls
            JOIN agents ON agents.name = calls.agent_name
            WHERE agents.assigned_qa_user_id = %s{date_clause}
            ORDER BY calls.created_at DESC LIMIT %s OFFSET %s
        ''', [user['id']] + date_params + [limit, offset])
    else:
        where_clause = date_clause.replace(' AND ', 'WHERE ', 1) if date_clause else ''
        c.execute(f'SELECT COUNT(*) FROM calls {where_clause}', date_params)
        total = c.fetchone()['count']
        c.execute(f'''
            SELECT call_id, agent_name, account_name, customer_account_id, caller_id,
                   created_at, duration, billed_minutes, call_duration_seconds, overall_score,
                   status, emotion, flags, call_end_first, call_notes, notes_score,
                   requires_human_review, agent_qos_tx, agent_qos_rx, customer_qos_tx,
                   customer_qos_rx, recording_url, error_message
            FROM calls
            {where_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
        ''', date_params + [limit, offset])
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
            ON CONFLICT (call_id) DO UPDATE SET
                recording_url = EXCLUDED.recording_url,
                call_duration_seconds = EXCLUDED.call_duration_seconds,
                billed_minutes = EXCLUDED.billed_minutes,
                duration = EXCLUDED.duration,
                call_notes = EXCLUDED.call_notes,
                call_end_first = EXCLUDED.call_end_first,
                agent_qos_tx = EXCLUDED.agent_qos_tx,
                agent_qos_rx = EXCLUDED.agent_qos_rx,
                customer_qos_tx = EXCLUDED.customer_qos_tx,
                customer_qos_rx = EXCLUDED.customer_qos_rx,
                call_dropped = EXCLUDED.call_dropped,
                status = CASE WHEN calls.status IN ('Failed','Processing') THEN 'Processing' ELSE calls.status END
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

            if is_processing_paused():
                print(f"[AutoProcess] ⏸️  Processing is paused — call {call_id} saved but not analyzed.")
                try:
                    conn_pause = get_db()
                    c_pause = conn_pause.cursor()
                    c_pause.execute("UPDATE calls SET status='Paused' WHERE call_id=%s", (call_id,))
                    conn_pause.commit()
                    conn_pause.close()
                except: pass
                return

            url_path = recording_url.split('?')[0]
            ext = os.path.splitext(url_path)[1].lower()
            if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
                ext = '.wav'

            UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            audio_path = os.path.join(UPLOAD_DIR, f"call_{call_id}_{int(time.time())}{ext}")

            try:
                download_recording(recording_url, audio_path, timeout=60, retries=2)
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
                error_str = str(e)[:500]
                print(f"[AutoProcess] ❌ {call_id} failed: {error_str}")
                try:
                    conn3 = get_db()
                    c3 = conn3.cursor()
                    c3.execute("UPDATE calls SET status='Failed', error_message=%s WHERE call_id=%s", (error_str, call_id))
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

@app.route('/api/compare-pipelines/<path:call_id>', methods=['POST'])
@require_admin
def compare_pipelines(call_id):
    """
    Runs BOTH the current Claude+Gemini pipeline AND the Gemini-only pipeline
    on the same call's recording, for side-by-side cost/quality comparison.
    Does NOT modify the call's real production scorecard — results are stored
    separately in pipeline_comparisons for review.
    """
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT recording_url, agent_name, call_end_first, call_notes, account_name, '
               'agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx FROM calls WHERE call_id=%s', (call_id,))
    call = c.fetchone()
    conn.close()
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    recording_url = (call.get('recording_url') or '').strip().rstrip(':').rstrip('/')
    ext = os.path.splitext(recording_url)[1] or '.wav'
    audio_path = os.path.join(UPLOAD_DIR, f"compare_{call_id}_{int(time.time())}{ext}")

    try:
        download_recording(recording_url, audio_path, timeout=60, retries=2)
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500

    from ai_engine import analyze_audio_with_gemini, score_call_with_claude, analyze_and_score_with_gemini_only, load_active_rules, load_active_exceptions

    rules = load_active_rules()
    exceptions = load_active_exceptions()
    common_args = dict(
        call_end_first=call.get('call_end_first', 'customer'),
        call_notes=call.get('call_notes', ''),
        account_name=call.get('account_name', ''),
        agent_qos_tx=call.get('agent_qos_tx', 'Good'),
        agent_qos_rx=call.get('agent_qos_rx', 'Good'),
        customer_qos_tx=call.get('customer_qos_tx', 'Good'),
        customer_qos_rx=call.get('customer_qos_rx', 'Good'),
    )

    results = {'call_id': call_id, 'agent_name': call.get('agent_name')}

    # Pipeline A: current production approach (Gemini listens, Claude scores)
    try:
        gemini_result = analyze_audio_with_gemini(audio_path)
        claude_result = score_call_with_claude(gemini_result, rules, exceptions=exceptions, **common_args)
        results['claude_gemini'] = {
            'overall_score': claude_result.get('overall_score'),
            'status': claude_result.get('status'),
            'flags': claude_result.get('flags', []),
            'rules_evaluation': claude_result.get('rules_evaluation', []),
            'coaching_notes': claude_result.get('coaching_notes', ''),
            'notes_score': claude_result.get('notes_score'),
        }
    except Exception as e:
        results['claude_gemini'] = {'error': str(e)}

    # Pipeline B: Gemini-only (single call does both listening and scoring)
    try:
        gemini_only_result = analyze_and_score_with_gemini_only(audio_path, rules, exceptions=exceptions, **common_args)
        sc = gemini_only_result.get('scorecard', {})
        results['gemini_only'] = {
            'overall_score': sc.get('overall_score'),
            'status': sc.get('status'),
            'flags': sc.get('flags', []),
            'rules_evaluation': sc.get('rules_evaluation', []),
            'coaching_notes': sc.get('coaching_notes', ''),
            'notes_score': sc.get('notes_score'),
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Compare] Gemini-only pipeline failed:\n{tb}")
        results['gemini_only'] = {'error': str(e)[:300] or 'Unknown error (empty exception)'}

    try: os.remove(audio_path)
    except: pass

    # Save comparison for later review
    try:
        conn2 = get_db()
        c2 = conn2.cursor()
        c2.execute('''INSERT INTO pipeline_comparisons (call_id, claude_gemini_scorecard, gemini_only_scorecard)
                      VALUES (%s, %s, %s)''',
                   (call_id, json.dumps(results.get('claude_gemini', {})), json.dumps(results.get('gemini_only', {}))))
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f"[Compare] Could not save comparison: {e}")

    return jsonify(results)

@app.route('/api/compare-pipelines', methods=['GET'])
def get_pipeline_comparisons():
    """Returns all saved side-by-side comparisons for review."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM pipeline_comparisons ORDER BY created_at DESC LIMIT 50')
    comparisons = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify({'comparisons': comparisons})

@app.route('/api/retry-call/<call_id>', methods=['POST'])
@require_admin
def retry_single_call(call_id):
    """Retry analysis for a single specific call."""
    if is_processing_paused():
        return jsonify({'error': 'Processing is currently paused. Resume processing first before retrying calls.'}), 409

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT call_id, agent_name, agent_extension, recording_url,
               call_duration_seconds, billed_minutes, caller_id,
               customer_account_id, account_name, call_end_first,
               agent_qos_tx, agent_qos_rx, customer_qos_tx, customer_qos_rx,
               call_notes, call_dropped
        FROM calls WHERE call_id = %s
    ''', (call_id,))
    call = c.fetchone()
    if not call:
        conn.close()
        return jsonify({'error': 'Call not found'}), 404

    # Reset status to Processing immediately so UI reflects it
    c.execute("UPDATE calls SET status='Processing', error_message=NULL, overall_score=0 WHERE call_id=%s", (call_id,))
    conn.commit()
    conn.close()

    import threading
    def process_single():
        from ai_engine import analyze_call as run_analysis
        UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        recording_url = (call.get('recording_url') or '').strip().rstrip(':').rstrip('/')
        ext = os.path.splitext(recording_url)[1] or '.wav'
        audio_path = os.path.join(UPLOAD_DIR, f"retry_{call_id}_{int(time.time())}{ext}")

        try:
            download_recording(recording_url, audio_path, timeout=60, retries=2)
            result = run_analysis(
                audio_path,
                call.get('agent_name', 'Unknown'),
                call_id,
                call_end_first=call.get('call_end_first', 'customer'),
                call_notes=call.get('call_notes', ''),
                account_name=call.get('account_name', ''),
                agent_qos_tx=call.get('agent_qos_tx', 'Good'),
                agent_qos_rx=call.get('agent_qos_rx', 'Good'),
                customer_qos_tx=call.get('customer_qos_tx', 'Good'),
                customer_qos_rx=call.get('customer_qos_rx', 'Good')
            )
            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute('''UPDATE calls SET duration=%s, overall_score=%s, confidence=%s,
                emotion=%s, status=%s, flags=%s, scorecard=%s, transcript=%s, summary=%s,
                emotion_delta=%s, requires_human_review=%s, human_review_reason=%s,
                age_concern=%s, coaching_notes=%s, positive_highlights=%s,
                call_dropped=%s, notes_score=%s, notes_feedback=%s, flagged_moments=%s,
                error_message=NULL WHERE call_id=%s''',
                (result.get('duration','--'), result['overall_score'], result.get('confidence',100),
                 result['emotion'], result['status'], result['flags'],
                 json.dumps(result.get('scorecard',{})), result.get('transcript',''),
                 result.get('summary',''), json.dumps(result.get('emotion_delta',{})),
                 result.get('requires_human_review',False), result.get('human_review_reason',''),
                 json.dumps(result.get('age_concern',{})), result.get('coaching_notes',''),
                 result.get('positive_highlights',''), result.get('call_dropped',False),
                 result.get('notes_score',0), result.get('notes_feedback',''),
                 json.dumps(result.get('flagged_moments',[])), call_id))
            # Save rule results
            c2.execute('DELETE FROM rule_results WHERE call_id=%s', (call_id,))
            sc = result.get('scorecard', {})
            for rule_eval in sc.get('rules_evaluation', []):
                c2.execute('''INSERT INTO rule_results (call_id, rule_description, category, severity, passed, confidence, evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    (call_id, rule_eval.get('rule',''), rule_eval.get('category',''),
                     rule_eval.get('severity','Warning'), rule_eval.get('passed',True),
                     rule_eval.get('confidence',80), rule_eval.get('evidence','')))
            conn2.commit()
            conn2.close()
            print(f"[RetryCall] ✅ {call_id} scored: {result['overall_score']}%")
        except Exception as e:
            error_str = str(e)[:500]
            print(f"[RetryCall] ❌ {call_id} failed: {error_str}")
            try:
                conn3 = get_db()
                c3 = conn3.cursor()
                c3.execute("UPDATE calls SET status='Failed', error_message=%s WHERE call_id=%s", (error_str, call_id))
                conn3.commit()
                conn3.close()
            except: pass
        finally:
            try: os.remove(audio_path)
            except: pass

    threading.Thread(target=process_single, daemon=True).start()
    return jsonify({'success': True, 'message': f'Retry started for call {call_id}', 'call_id': call_id})


@app.route('/api/retry-stuck', methods=['POST'])
@require_admin
def retry_stuck_calls():
    global retry_log, retry_running
    import threading

    if is_processing_paused():
        return jsonify({'error': 'Processing is currently paused. Resume processing first via the pause toggle before retrying calls.'}), 409

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
        ORDER BY created_at DESC
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
                    download_recording(recording_url, audio_path, timeout=60, retries=1)
                    size_bytes = os.path.getsize(audio_path)
                    size_mb = round(size_bytes / 1024 / 1024, 1)

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
                error_str = str(e)[:500]
                retry_log.append({'time': datetime.now().strftime('%I:%M:%S %p'), 'msg': f'❌ {agent} #{call_id[-8:]} — {error_str[:80]}', 'type': 'error'})
                try:
                    conn3 = get_db()
                    c3 = conn3.cursor()
                    c3.execute("UPDATE calls SET status='Failed', error_message=%s WHERE call_id=%s", (error_str, call_id))
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
            download_recording(recording_url, audio_path, timeout=60, retries=1)
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

@app.route('/api/learned-exceptions', methods=['GET'])
@require_manager
def list_learned_exceptions():
    """List all learned exceptions with their rule and active status."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT le.*, r.active AS rule_active
        FROM learned_exceptions le
        LEFT JOIN rules r ON le.rule_id = r.id
        ORDER BY le.created_at DESC
    ''')
    exceptions = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'exceptions': exceptions})

@app.route('/api/learned-exceptions/<int:exc_id>/toggle', methods=['POST'])
@require_manager
def toggle_learned_exception(exc_id):
    """Turn a learned exception on or off without deleting it."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT active FROM learned_exceptions WHERE id=%s', (exc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    new_active = not row['active']
    c.execute('UPDATE learned_exceptions SET active=%s WHERE id=%s RETURNING *', (new_active, exc_id))
    updated = dict(c.fetchone())
    conn.commit()
    conn.close()
    return jsonify(updated)

@app.route('/api/manager-queue', methods=['GET'])
@require_manager
def get_manager_queue():
    """Returns all flag reviews marked as AI mistakes that are pending manager decision."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT fr.*, c.agent_name, c.account_name, c.recording_url, c.overall_score
        FROM flag_reviews fr
        LEFT JOIN calls c ON c.call_id = fr.call_id
        WHERE fr.marked_ai_mistake = TRUE AND fr.manager_status = 'pending'
        ORDER BY fr.reviewed_at DESC
    ''')
    queue = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'queue': queue})

@app.route('/api/manager-queue/count', methods=['GET'])
@require_login
def get_manager_queue_count():
    """Lightweight count for the sidebar badge."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM flag_reviews WHERE marked_ai_mistake=TRUE AND manager_status='pending'")
    count = c.fetchone()[0]
    conn.close()
    return jsonify({'count': count})

@app.route('/api/manager-suggest-rule-fix', methods=['POST'])
@require_manager
def manager_suggest_rule_fix():
    """
    AI-assisted rule rewriter: given the rule that mis-fired and a description of the
    situation it got wrong, ask Claude to propose a more precise rule wording that would
    NOT flag this situation while preserving the rule's original intent.
    """
    data = request.json or {}
    current_rule = data.get('current_rule', '')
    situation = data.get('situation', '')
    if not current_rule:
        return jsonify({'error': 'current_rule required'}), 400

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are helping refine a call-center QA rule that produced a false positive.

CURRENT RULE: "{current_rule}"

SITUATION THE AI WRONGLY FLAGGED: {situation or "(manager did not describe a specific situation; infer a likely false-positive scenario for this rule)"}

Propose a single improved version of this rule that:
1. Keeps the rule's original protective intent
2. Adds just enough precision so this specific kind of situation is NOT wrongly flagged
3. Stays one clear sentence, written the same plain style as the original

Respond ONLY with valid JSON:
{{"suggested_rule":"the improved rule text","explanation":"one sentence on what changed and why this stops the false positive"}}"""

        resp = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=500,
            messages=[{'role':'user','content':prompt}]
        )
        text = resp.content[0].text.strip()
        if text.startswith('```'):
            import re as _re
            text = _re.sub(r'^```(?:json)?\s*','',text); text = _re.sub(r'\s*```$','',text)
        suggestion = json.loads(text)
        return jsonify(suggestion)
    except Exception as e:
        return jsonify({'error': f'Could not generate suggestion: {str(e)[:200]}'}), 500

@app.route('/api/manager-decision/<int:review_id>', methods=['POST'])
@require_manager
def manager_decision(review_id):
    """
    Manager approves or rejects an AI-mistake flag.
    On approve, the manager chooses resolution_type:
      - 'rule_fix': update the rule's wording (data.new_rule_text + rule_id)
      - 'exception': add a learned exception (data.exception_text + rule_id)
    On reject: the flag stands, nothing learned.
    """
    user = current_user()
    data = request.json or {}
    decision = data.get('decision')  # 'approve' or 'reject'
    resolution_type = data.get('resolution_type')  # 'rule_fix' or 'exception' (when approving)
    manager_note = (data.get('manager_note') or '').strip()

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM flag_reviews WHERE id=%s', (review_id,))
    review = c.fetchone()
    if not review:
        conn.close()
        return jsonify({'error': 'Review not found'}), 404

    if decision == 'reject':
        c.execute('''UPDATE flag_reviews SET manager_status='rejected', manager_id=%s, manager_note=%s
                     WHERE id=%s''', (user.get('id') if user else None, manager_note, review_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'status': 'rejected'})

    # decision == 'approve' — apply the chosen resolution
    rule_id = data.get('rule_id')

    # If rule_id wasn't provided (flags don't always carry one), resolve it from the
    # flag's stored rule/title text by matching against the rules table.
    if not rule_id:
        flag_text = (review.get('flag_rule') or review.get('flag_title') or '').strip()
        if flag_text:
            # Try exact match first, then a loose contains match
            c.execute('SELECT id FROM rules WHERE description = %s LIMIT 1', (flag_text,))
            rr = c.fetchone()
            if not rr:
                c.execute("SELECT id FROM rules WHERE description ILIKE %s LIMIT 1", (f'%{flag_text[:40]}%',))
                rr = c.fetchone()
            if rr:
                rule_id = rr['id']

    if resolution_type == 'rule_fix':
        new_rule_text = (data.get('new_rule_text') or '').strip()
        if not new_rule_text:
            conn.close()
            return jsonify({'error': 'rule_fix requires new_rule_text'}), 400
        if not rule_id:
            conn.close()
            return jsonify({'error': 'Could not match this flag to a specific rule. Use "Add exception" instead, or fix the rule manually in Rules Engine.'}), 400
        c.execute('UPDATE rules SET description=%s WHERE id=%s', (new_rule_text, rule_id))

    elif resolution_type == 'exception':
        exception_text = (data.get('exception_text') or '').strip()
        if not exception_text:
            conn.close()
            return jsonify({'error': 'exception requires exception_text'}), 400
        # Look up rule description for storage
        rule_desc = ''
        if rule_id:
            c.execute('SELECT description FROM rules WHERE id=%s', (rule_id,))
            rr = c.fetchone()
            rule_desc = rr['description'] if rr else ''
        else:
            # No rule matched — store the flag text so the text-fallback in load_active_exceptions can still work
            rule_desc = (review.get('flag_rule') or review.get('flag_title') or '')
        c.execute('''INSERT INTO learned_exceptions
            (rule_id, rule_description, exception_text, source_call_id, approved_by, active)
            VALUES (%s,%s,%s,%s,%s,TRUE)''',
            (rule_id, rule_desc, exception_text, review['call_id'], user.get('id') if user else None))
    else:
        conn.close()
        return jsonify({'error': 'approve requires resolution_type of rule_fix or exception'}), 400

    c.execute('''UPDATE flag_reviews SET manager_status='approved', manager_id=%s, manager_note=%s, resolution_type=%s
                 WHERE id=%s''', (user.get('id') if user else None, manager_note, resolution_type, review_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'status': 'approved', 'resolution_type': resolution_type})

@app.route('/api/flag-reviews/<call_id>', methods=['GET'])
@require_login
def get_flag_reviews(call_id):
    """Get all saved flag reviews for a call."""
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM flag_reviews WHERE call_id=%s ORDER BY flag_index', (call_id,))
    reviews = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'reviews': reviews})

@app.route('/api/flag-reviews/<call_id>', methods=['POST'])
@require_login
def save_flag_reviews(call_id):
    """
    Save per-flag resolution notes + AI-mistake flags for a call.
    Expects: {reviews: [{flag_index, flag_title, flag_rule, resolution_note, marked_ai_mistake}]}
    Every flag must have a non-empty note unless marked as an AI mistake.
    """
    user = current_user()
    data = request.json or {}
    reviews = data.get('reviews', [])

    # Validate: each flag needs either a note OR to be marked an AI mistake
    for r in reviews:
        note = (r.get('resolution_note') or '').strip()
        is_mistake = r.get('marked_ai_mistake', False)
        if not note and not is_mistake:
            return jsonify({'error': f"Flag '{r.get('flag_title','?')}' needs a resolution note or must be marked as an AI mistake."}), 400

    conn = get_db()
    c = conn.cursor()
    # Clear existing reviews for this call, then re-insert (full replace on save)
    c.execute('DELETE FROM flag_reviews WHERE call_id=%s', (call_id,))
    for r in reviews:
        is_mistake = bool(r.get('marked_ai_mistake', False))
        manager_status = 'pending' if is_mistake else 'none'
        c.execute('''INSERT INTO flag_reviews
            (call_id, flag_index, flag_title, flag_rule, resolution_note, marked_ai_mistake,
             reviewed_by, reviewer_name, manager_status, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)''',
            (call_id, r.get('flag_index'), r.get('flag_title',''), r.get('flag_rule',''),
             (r.get('resolution_note') or '').strip(), is_mistake,
             user.get('id') if user else None, user.get('full_name') or user.get('username') if user else 'Unknown',
             manager_status))
    conn.commit()
    conn.close()
    pending_count = sum(1 for r in reviews if r.get('marked_ai_mistake'))
    return jsonify({'success': True, 'saved': len(reviews), 'sent_to_manager': pending_count})

@app.route('/api/processing-status', methods=['GET'])
def get_processing_status():
    return jsonify({'paused': is_processing_paused()})

@app.route('/api/processing-status', methods=['POST'])
@require_admin
def set_processing_status():
    data = request.json
    paused = bool(data.get('paused', False))
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO app_settings (key, value, updated_at) VALUES ('processing_paused', %s, CURRENT_TIMESTAMP)
                 ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=CURRENT_TIMESTAMP''',
              ('true' if paused else 'false',))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'paused': paused})

@app.route('/api/network-diagnostic', methods=['GET'])
def network_diagnostic():
    """
    Tests DNS resolution, TCP connection, and HTTP fetch separately against the
    recording server, plus comparison tests against other hosts/ports, so we can
    pinpoint exactly which layer is failing from Azure and whether it's specific
    to this host or a broader outbound restriction.
    """
    import socket
    import time as _time

    target_host = 'main.getremail.com'
    test_url = request.args.get('url', 'http://main.getremail.com/recordings/1781754701.6117618.wav')

    results = {}

    # Test 1: DNS resolution of target host
    try:
        start = _time.time()
        ip = socket.gethostbyname(target_host)
        results['dns_target'] = f'OK \u2014 {target_host} resolves to {ip} in {round(_time.time()-start,2)}s'
    except Exception as e:
        results['dns_target'] = f'FAIL: {type(e).__name__}: {str(e)}'

    # Test 2: Raw TCP connection to target host, port 80
    try:
        start = _time.time()
        sock = socket.create_connection((target_host, 80), timeout=10)
        sock.close()
        results['tcp_target_port80'] = f'OK \u2014 connected in {round(_time.time()-start,2)}s'
    except Exception as e:
        results['tcp_target_port80'] = f'FAIL: {type(e).__name__}: {str(e)}'

    # Test 3: Raw TCP connection to target host, port 443 (does HTTPS work where HTTP doesn't?)
    try:
        start = _time.time()
        sock = socket.create_connection((target_host, 443), timeout=10)
        sock.close()
        results['tcp_target_port443'] = f'OK \u2014 connected in {round(_time.time()-start,2)}s'
    except Exception as e:
        results['tcp_target_port443'] = f'FAIL: {type(e).__name__}: {str(e)}'

    # Test 4: Raw TCP to a totally unrelated, well-known host on port 80 (is ALL outbound port 80 blocked, or just this host?)
    try:
        start = _time.time()
        sock = socket.create_connection(('example.com', 80), timeout=10)
        sock.close()
        results['tcp_unrelated_host_port80'] = f'OK \u2014 connected to example.com:80 in {round(_time.time()-start,2)}s'
    except Exception as e:
        results['tcp_unrelated_host_port80'] = f'FAIL: {type(e).__name__}: {str(e)}'

    # Test 5: Full HTTP GET attempt on the actual recording URL
    try:
        start = _time.time()
        req = urllib.request.Request(test_url, headers={'User-Agent': 'VoiceGuard/1.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(1024)
            status_code = resp.status
        results['http_get_target'] = f'OK \u2014 HTTP {status_code}, got {len(data)} bytes in {round(_time.time()-start,2)}s'
    except urllib.error.HTTPError as e:
        results['http_get_target'] = f'FAIL: HTTP {e.code} \u2014 {e.reason}'
    except Exception as e:
        results['http_get_target'] = f'FAIL: {type(e).__name__}: {str(e)} after {round(_time.time()-start,2)}s'

    # Test 6: Outbound IP this Azure instance is currently using
    try:
        req = urllib.request.Request('https://api.ipify.org?format=json')
        with urllib.request.urlopen(req, timeout=10) as resp:
            results['azure_outbound_ip'] = json.loads(resp.read())
    except Exception as e:
        results['azure_outbound_ip'] = f'Could not determine: {str(e)}'

    return jsonify(results)

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

    # Test 4: Can we reach the recording server directly?
    direct_test_url = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT recording_url, call_id FROM calls WHERE recording_url IS NOT NULL AND recording_url != '' ORDER BY created_at DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            direct_test_url = row[0].strip().rstrip(':').rstrip('/')
            import time as _time
            start = _time.time()
            req = urllib.request.Request(direct_test_url, method='HEAD', headers={'User-Agent': 'VoiceGuard/1.0'})
            urllib.request.urlopen(req, timeout=15)
            elapsed = round(_time.time() - start, 1)
            results['audio_url_direct'] = f'OK — reachable in {elapsed}s: ...{direct_test_url[-40:]}'
        else:
            results['audio_url_direct'] = 'No calls with recording URL'
    except urllib.error.HTTPError as e:
        results['audio_url_direct'] = f'FAIL: Server reachable but returned HTTP {e.code} (URL or auth may be wrong)'
    except Exception as e:
        results['audio_url_direct'] = f'FAIL (expected if relay configured): {type(e).__name__}: {str(e)}'

    # Test 5: Can we reach the recording server through the relay (Canada Central)?
    if RELAY_URL and direct_test_url:
        try:
            import time as _time
            start = _time.time()
            relay_fetch_url = f"{RELAY_URL.rstrip('/')}/fetch?url={urllib.parse.quote(direct_test_url, safe='')}"
            req = urllib.request.Request(relay_fetch_url, headers={'X-Relay-Secret': RELAY_SECRET})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(2048)
            elapsed = round(_time.time() - start, 1)
            results['audio_url_via_relay'] = f'OK — relay fetched {len(data)}+ bytes in {elapsed}s'
        except urllib.error.HTTPError as e:
            results['audio_url_via_relay'] = f'FAIL: Relay returned HTTP {e.code}'
        except Exception as e:
            results['audio_url_via_relay'] = f'FAIL: {type(e).__name__}: {str(e)}'
    elif not RELAY_URL:
        results['audio_url_via_relay'] = 'RELAY_URL not configured — set it in App Settings to enable'
    else:
        results['audio_url_via_relay'] = 'Skipped — no recording URL available to test'

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
