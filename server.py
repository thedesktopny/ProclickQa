from flask import Flask, request, jsonify, session, send_from_directory, send_file, make_response
from flask_cors import CORS
import hashlib
import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
import urllib.request
import urllib.error
import urllib.parse

# Read once at startup. Two places used this name without it ever being defined,
# so both failed with "name 'ANTHROPIC_API_KEY' is not defined" the moment they
# were called — the notes questions and the QA rule-refinement helper.
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

# Claude pricing, same figures the call scoring uses (dollars per million tokens)
CLAUDE_INPUT_COST_PER_M = 3.00
CLAUDE_OUTPUT_COST_PER_M = 15.00

# Claude reads a 200,000-token window — roughly 790,000 characters. The default
# below leaves plenty of headroom and keeps a question at a few cents; raise
# notes_char_budget in settings to read more per question, at more cost.
NOTES_CHAR_BUDGET_DEFAULT = 400_000     # ~100k tokens, ~$0.30 a question
MAX_NOTES_HARD = 8000                   # a sane ceiling on the database read

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
        CREATE TABLE IF NOT EXISTS skinblock_jobs (
            id SERIAL PRIMARY KEY,
            agent_ext TEXT NOT NULL,
            file_name TEXT,
            original_image TEXT,
            covered_image TEXT,
            images_purged BOOLEAN DEFAULT FALSE,
            person_found BOOLEAN,
            faces_found INTEGER DEFAULT 0,
            manual_bars INTEGER DEFAULT 0,
            coverage_pct NUMERIC,
            settings_used TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute("CREATE INDEX IF NOT EXISTS skinblock_ext_idx ON skinblock_jobs (agent_ext, created_at)")

    c.execute('''
        CREATE TABLE IF NOT EXISTS shift_adjustments (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            shift_date DATE NOT NULL,
            block_no INTEGER DEFAULT 1,
            adjusted_in TIMESTAMP,
            adjusted_out TIMESTAMP,
            adjusted_break_minutes NUMERIC,
            original_in TIMESTAMP,
            original_out TIMESTAMP,
            original_break_minutes NUMERIC,
            reason TEXT,
            adjusted_by INTEGER,
            adjusted_by_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, shift_date, block_no)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS agent_rates (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            hourly_rate NUMERIC(10,2) NOT NULL DEFAULT 0,
            effective_from TIMESTAMP NOT NULL DEFAULT '2000-01-01',
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS ot_multiplier_periods (
            id SERIAL PRIMARY KEY,
            starts_at TIMESTAMP NOT NULL,
            ends_at TIMESTAMP,
            multiplier NUMERIC(4,2) NOT NULL DEFAULT 2.0,
            note TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS recurring_schedules (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            day_of_week INTEGER NOT NULL,
            scheduled_in_time TEXT NOT NULL,
            scheduled_out_time TEXT NOT NULL,
            overnight BOOLEAN DEFAULT FALSE,
            break_minutes INTEGER DEFAULT 0,
            active BOOLEAN DEFAULT TRUE,
            block_no INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, day_of_week, block_no)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS skinblock_detector_log (
            id SERIAL PRIMARY KEY,
            agent_ext TEXT,
            mode TEXT,
            reason TEXT,
            backend TEXT,
            isolated BOOLEAN,
            threads INTEGER,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # CMS-sourced clock events live in their OWN table. The point is that the
    # existing upload-driven report keeps working untouched while the CMS
    # version is proven — same columns, so the same code reads either one.
    c.execute('''
        CREATE TABLE IF NOT EXISTS clocker_events_cms (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            event_time TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            break_minutes NUMERIC,
            break_reason TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, event_time, status)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS phone_status_snapshots (
            id SERIAL PRIMARY KEY,
            snapshot_at TIMESTAMP NOT NULL,
            agent_ext TEXT,
            employee_id INTEGER,
            phone_status INTEGER,
            cms_status INTEGER,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(snapshot_at, agent_ext)
        )
    ''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_phone_snap_emp_time
                 ON phone_status_snapshots (employee_id, snapshot_at)''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS week_schedules (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            week_start DATE NOT NULL,
            day_of_week INTEGER NOT NULL,
            scheduled_in_time TEXT NOT NULL,
            scheduled_out_time TEXT NOT NULL,
            overnight BOOLEAN DEFAULT FALSE,
            break_minutes INTEGER DEFAULT 0,
            active BOOLEAN DEFAULT TRUE,
            block_no INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, week_start, day_of_week, block_no)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS agent_schedules (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            shift_date DATE NOT NULL,
            scheduled_in TIMESTAMP NOT NULL,
            scheduled_out TIMESTAMP NOT NULL,
            break_minutes INTEGER DEFAULT 0,
            notes TEXT,
            block_no INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, shift_date, block_no)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS clocker_events (
            id SERIAL PRIMARY KEY,
            employee_name TEXT NOT NULL,
            event_time TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            break_minutes NUMERIC,
            break_reason TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_name, event_time, status)
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
    # Commit all CREATE TABLE work before running migrations, so a failing
    # migration can't roll back the schema we just created.
    try: conn.commit()
    except Exception: pass

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
        # ── Rate history: rates are effective-dated so past shifts keep their old rate
        "ALTER TABLE agent_rates ADD COLUMN IF NOT EXISTS effective_from TIMESTAMP DEFAULT '2000-01-01'",
        "ALTER TABLE agent_rates ADD COLUMN IF NOT EXISTS note TEXT",
        "ALTER TABLE agent_rates DROP CONSTRAINT IF EXISTS agent_rates_employee_name_key",
        "CREATE INDEX IF NOT EXISTS agent_rates_lookup ON agent_rates (employee_name, effective_from)",
        # ── Split-shift support: an agent can have more than one scheduled block per day
        # (e.g. 7am-12pm and 7pm-12am). block_no distinguishes them.
        "ALTER TABLE agent_schedules ADD COLUMN IF NOT EXISTS block_no INTEGER DEFAULT 1",
        "ALTER TABLE recurring_schedules ADD COLUMN IF NOT EXISTS block_no INTEGER DEFAULT 1",
        "ALTER TABLE agent_schedules DROP CONSTRAINT IF EXISTS agent_schedules_employee_name_shift_date_key",
        "ALTER TABLE recurring_schedules DROP CONSTRAINT IF EXISTS recurring_schedules_employee_name_day_of_week_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS agent_sched_unique ON agent_schedules (employee_name, shift_date, block_no)",
        "CREATE UNIQUE INDEX IF NOT EXISTS recurring_sched_unique ON recurring_schedules (employee_name, day_of_week, block_no)",
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
            conn.commit()
        except Exception as e:
            # A failed statement aborts the whole Postgres transaction — roll back so
            # the following migrations (and the table creates above) still commit.
            try: conn.rollback()
            except Exception: pass
            print(f"[init_db] migration skipped: {str(e)[:120]}")

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

    # Cost constants (match your real measured per-call averages)
    CLAUDE_INPUT_PER_M = 3.00
    CLAUDE_OUTPUT_PER_M = 15.00
    GEMINI_INPUT_PER_M = 0.30
    GEMINI_OUTPUT_PER_M = 3.50
    GEMINI_AUDIO_PER_MIN = 0.001

    audio_mb = 0
    try: audio_mb = os.path.getsize(audio_path) / 1024 / 1024
    except: pass
    # Rough audio-minutes proxy from file size (WAV ~1MB/min is a conservative default)
    est_audio_min = max(1, round(audio_mb))

    # Pipeline A: current production approach (Gemini listens, Claude scores)
    try:
        gemini_result = analyze_audio_with_gemini(audio_path)
        claude_result = score_call_with_claude(gemini_result, rules, exceptions=exceptions, **common_args)
        # Estimate cost: Gemini audio-in + Claude scoring tokens
        cg_transcript = gemini_result.get('transcript', '') or ''
        claude_in_tokens = (len(cg_transcript) + 4000) / 4   # transcript + prompt overhead
        claude_out_tokens = len(json.dumps(claude_result)) / 4
        cg_cost = (est_audio_min * GEMINI_AUDIO_PER_MIN) \
                  + (claude_in_tokens/1_000_000 * CLAUDE_INPUT_PER_M) \
                  + (claude_out_tokens/1_000_000 * CLAUDE_OUTPUT_PER_M)
        results['claude_gemini'] = {
            'overall_score': claude_result.get('overall_score'),
            'status': claude_result.get('status'),
            'flags': claude_result.get('flags', []),
            'rules_evaluation': claude_result.get('rules_evaluation', []),
            'coaching_notes': claude_result.get('coaching_notes', ''),
            'notes_score': claude_result.get('notes_score'),
            'cost_usd': round(cg_cost, 4),
        }
    except Exception as e:
        results['claude_gemini'] = {'error': str(e)}
        cg_cost = 0

    # Pipeline B: Gemini-only (single call does both listening and scoring)
    try:
        gemini_only_result = analyze_and_score_with_gemini_only(audio_path, rules, exceptions=exceptions, **common_args)
        sc = gemini_only_result.get('scorecard', {})
        go_out_tokens = len(json.dumps(sc)) / 4
        go_cost = (est_audio_min * GEMINI_AUDIO_PER_MIN) \
                  + (go_out_tokens/1_000_000 * GEMINI_OUTPUT_PER_M)
        results['gemini_only'] = {
            'overall_score': sc.get('overall_score'),
            'status': sc.get('status'),
            'flags': sc.get('flags', []),
            'rules_evaluation': sc.get('rules_evaluation', []),
            'coaching_notes': sc.get('coaching_notes', ''),
            'notes_score': sc.get('notes_score'),
            'cost_usd': round(go_cost, 4),
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Compare] Gemini-only pipeline failed:\n{tb}")
        results['gemini_only'] = {'error': str(e)[:300] or 'Unknown error (empty exception)'}
        go_cost = 0

    try: os.remove(audio_path)
    except: pass

    # Save comparison for later review (now with costs)
    try:
        conn2 = get_db()
        c2 = conn2.cursor()
        c2.execute('''INSERT INTO pipeline_comparisons (call_id, claude_gemini_scorecard, gemini_only_scorecard, claude_gemini_cost, gemini_only_cost)
                      VALUES (%s, %s, %s, %s, %s)''',
                   (call_id, json.dumps(results.get('claude_gemini', {})), json.dumps(results.get('gemini_only', {})),
                    cg_cost, go_cost))
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

# ─── TIME REPORT / ATTENDANCE ─────────────────────────────────────────────────
def _build_shifts_from_events(events):
    """
    Reconstruct shifts from raw clocker events for ONE employee.
    events: list of dicts with keys event_time (datetime), status, break_minutes

    Rule (confirmed with CMS side): take only the EARLIEST 'In' of a shift and ignore
    every later 'In' — regardless of how far apart they are — UNLESS an 'Out' or an
    'OnBreak' happened in between. Duplicate 'In' rows can be seconds OR hours apart,
    so elapsed time is not a valid signal; only an intervening Out/OnBreak makes a
    later 'In' meaningful (a genuine re-login or a return from break).

    Also handles shifts crossing midnight and reports whose window starts mid-shift.
    """
    events = sorted(events, key=lambda e: e['event_time'])

    shifts, cur = [], None
    on_break = False  # True between an OnBreak and the 'In' that ends it

    for e in events:
        st, t = e['status'], e['event_time']

        if st == 'In':
            if cur is None:
                # First login of a new shift
                cur = {'login': t, 'logout': None, 'breaks': [], 'partial': False}
                on_break = False
            elif on_break:
                # Returning from a break — close it out and measure the ACTUAL duration
                # from the timestamps, not the minutes the agent declared.
                if cur['breaks']:
                    b = cur['breaks'][-1]
                    b['end'] = t
                    b['minutes'] = (t - b['start']).total_seconds() / 60
                    b['closed'] = True
                on_break = False
            else:
                # Repeat 'In' with no Out/OnBreak in between → duplicate, ignore entirely
                continue

        elif st == 'OnBreak':
            if cur is None:
                # Break with no preceding login = report window began mid-shift
                cur = {'login': t, 'logout': None, 'breaks': [], 'partial': True}
            cur['breaks'].append({
                'start': t,
                'end': None,
                'declared_minutes': float(e.get('break_minutes') or 0),
                'minutes': float(e.get('break_minutes') or 0),  # fallback until closed
                'closed': False,
            })
            on_break = True

        elif st == 'Out':
            if cur is not None:
                # If they clocked out while still on break, end the break at logout
                if on_break and cur['breaks']:
                    b = cur['breaks'][-1]
                    b['end'] = t
                    b['minutes'] = (t - b['start']).total_seconds() / 60
                    b['closed'] = True
                cur['logout'] = t
                shifts.append(cur)
                cur = None
                on_break = False

    if cur:
        shifts.append(cur)  # still clocked in
    return shifts

DEFAULT_OT_MULTIPLIER = 1.5

def _subtract_intervals(base, subs):
    """base: (start,end). subs: list of (start,end). Returns list of remaining intervals."""
    result = [base]
    for s_start, s_end in subs:
        nxt = []
        for b_start, b_end in result:
            if s_end <= b_start or s_start >= b_end:
                nxt.append((b_start, b_end)); continue
            if s_start > b_start: nxt.append((b_start, s_start))
            if s_end < b_end: nxt.append((s_end, b_end))
        result = nxt
    return result

def _split_at(interval, boundaries):
    """Split (start,end) at any boundary datetimes that fall strictly inside it."""
    start, end = interval
    pts = sorted({start, end} | {b for b in boundaries if b and start < b < end})
    return [(pts[i], pts[i+1]) for i in range(len(pts)-1)]

def _rate_at(t, rate_history):
    """Hourly rate in force at time t. rate_history must be sorted by effective_from."""
    rate = 0.0
    for entry in rate_history:
        if entry['effective_from'] <= t:
            rate = float(entry['hourly_rate'])
        else:
            break
    return rate

def _mult_at(t, ot_periods):
    """Overtime multiplier in force at time t (latest matching period wins)."""
    mult = DEFAULT_OT_MULTIPLIER
    for p in ot_periods:
        if p['starts_at'] <= t and (p['ends_at'] is None or t < p['ends_at']):
            mult = float(p['multiplier'])
    return mult

def _compute_pay(segments, sched_window, rate_history, ot_periods):
    """
    Splits actual worked time into regular vs overtime and prices it, honouring
    both effective-dated pay-rate changes and manager-declared overtime periods.
    sched_window: (sched_in, sched_out) or None for unscheduled work (all overtime).
    """
    worked = []
    for seg in segments:
        if not seg['logout']:
            continue
        breaks = []
        for b in seg['breaks']:
            b_start = b.get('start')
            b_end = b.get('end')
            if b_start and b_end:
                breaks.append((b_start, b_end))          # measured from timestamps
            elif b_start:
                mins = float(b.get('minutes') or 0)      # unclosed break — fall back to declared
                if mins > 0:
                    breaks.append((b_start, b_start + timedelta(minutes=mins)))
        worked.extend(_subtract_intervals((seg['login'], seg['logout']), breaks))

    # Every point where the rate or the overtime multiplier could change
    boundaries = [e['effective_from'] for e in rate_history]
    for p in ot_periods:
        boundaries.append(p['starts_at'])
        if p['ends_at']: boundaries.append(p['ends_at'])
    if sched_window:
        boundaries.extend([sched_window[0], sched_window[1]])

    reg_secs = reg_pay = 0.0
    ot_secs = ot_pay = 0.0
    by_mult, rates_used = {}, set()

    for w in worked:
        for piece in _split_at(w, boundaries):
            secs = (piece[1] - piece[0]).total_seconds()
            if secs <= 0: continue
            hours = secs / 3600
            rate = _rate_at(piece[0], rate_history)
            rates_used.add(rate)
            in_schedule = sched_window and sched_window[0] <= piece[0] < sched_window[1]
            if in_schedule:
                reg_secs += secs
                reg_pay += hours * rate
            else:
                mult = _mult_at(piece[0], ot_periods)
                ot_secs += secs
                ot_pay += hours * rate * mult
                by_mult[mult] = by_mult.get(mult, 0) + hours

    return {
        'regular_hours': round(reg_secs/3600, 2),
        'overtime_hours': round(ot_secs/3600, 2),
        'regular_pay': round(reg_pay, 2),
        'overtime_pay': round(ot_pay, 2),
        'total_pay': round(reg_pay + ot_pay, 2),
        'ot_breakdown': {str(k): round(v, 2) for k, v in sorted(by_mult.items())},
        'hourly_rate': max(rates_used) if rates_used else 0,
    }

# ─── SKIN BLOCK (photo people-cover tool) ─────────────────────────────────────
SKINBLOCK_DEFAULTS = {
    # --- how the covering looks ---
    'cover_mode': 'blend',      # 'blend' = fill each area with its own averaged
                                # tone so it sits in the picture; 'solid' = flat colour
    'cover_color': '#000000',   # used when cover_mode is 'solid'
    'cover_hair': False,        # hair stays visible; set True to cover the whole head
    'smooth_shapes': 0.6,       # 0 = trace exactly, higher = rounder; above ~1
                                # the rounding starts swallowing thin straps
    'skin_bias': 0.35,          # >0 covers a little more readily (catches missed skin)
    'max_windows': 60,          # ceiling on model runs per photo
    'extend_by_colour': True,   # reclaim bare torso the model labels as clothing
    'skin_colour_tolerance': 12, # how closely a pixel must match the person's own skin
    'skin_reach_px': 90,        # how far covering may extend from confirmed skin
    'coarse_enough': 0.28,      # subject taller than this share of the photo -> one pass
    'skin_grow_limit': 2.4,     # discard growth that balloons past this multiple
    'second_pass': True,        # second, shifted grid so nobody is missed by
                                # falling across a window edge (doubles the time)
    'skin_pad': 0,              # grow past the model's skin edge (0 = exact)
    'time_budget': 110,         # seconds of model time per photo; Azure aborts at ~230
    'edge_padding': 5,          # % of image dimension to expand the mask
    'face_grow_x': 0.45,        # extra width around detected faces
    'face_grow_y': 0.55,        # extra height around detected faces
    'shrink_large': True,
    'max_dimension': 2000,
    'require_extension': True,
    'retention_days': 30,
    # selfie_multiclass classes: 1=hair 2=body-skin 3=face-skin 4=clothes 5=accessories.
    # Default covers skin only, so a dress or shoes stay visible.
    'cover_classes': [2, 3],
    'tiles': 1,                 # NxN grid; raise for screenshots/collages
    'cover_faces': False,       # face rectangles also cover hair and collar
    # Per-pixel skin-colour pass. Unlike the segmenter this works at any size,
    # so it catches faces and hands in small grid thumbnails.
    'skin_color_detect': True,
    'skin_sensitivity': 0,      # 0-25, widens the skin colour window
    'noise_removal': 1,         # erode radius to drop stray warm pixels
}

def _skinblock_settings():
    s = dict(SKINBLOCK_DEFAULTS)
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key='skinblock_settings'")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            s.update(json.loads(row[0]))
    except Exception as e:
        print(f"[skinblock] settings unavailable: {str(e)[:120]}")
    return s

@app.route('/api/skinblock/settings', methods=['GET'])
def get_skinblock_settings():
    """Public — the tool needs these to run. No auth so any agent can load the page."""
    return jsonify(_skinblock_settings())

@app.route('/api/skinblock/settings', methods=['POST'])
@require_manager
def save_skinblock_settings():
    d = request.json or {}
    s = _skinblock_settings()
    for k in SKINBLOCK_DEFAULTS:
        if k in d:
            s[k] = d[k]
    conn = get_db(); c = conn.cursor()
    c.execute('''INSERT INTO app_settings (key, value, updated_at)
                 VALUES ('skinblock_settings', %s, CURRENT_TIMESTAMP)
                 ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=CURRENT_TIMESTAMP''',
              (json.dumps(s),))
    conn.commit(); conn.close()
    return jsonify(s)

@app.route('/api/skinblock/submit', methods=['POST'])
def skinblock_submit():
    """
    Public — records one processed photo. Called by the tool after it covers an image.
    Stores both images for the retention window, then only the log record survives.
    """
    d = request.json or {}
    ext = (d.get('agent_ext') or '').strip()
    if not re.fullmatch(r'\d{3}', ext):
        return jsonify({'error': 'A 3-digit extension is required'}), 400

    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''INSERT INTO skinblock_jobs
        (agent_ext, file_name, original_image, covered_image, person_found,
         faces_found, manual_bars, coverage_pct, settings_used)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at''',
        (ext, d.get('file_name',''), d.get('original_image'), d.get('covered_image'),
         bool(d.get('person_found')), int(d.get('faces_found') or 0),
         int(d.get('manual_bars') or 0), d.get('coverage_pct'),
         json.dumps(d.get('settings_used') or {})))
    row = dict(c.fetchone()); conn.commit(); conn.close()
    return jsonify({'success': True, 'id': row['id']})

@app.route('/api/outbound-ip', methods=['GET'])
@require_manager
def outbound_ip():
    """The addresses this server appears as when it connects OUT to somewhere
    else — what Igor needs to allowlist on the phone system's database.

    Azure gives an App Service several outbound addresses, not one, and the set
    changes if the plan's tier changes. So this returns the live address AND the
    full list Azure says is possible: allowlist all of them, or the connection
    will work until the day it silently doesn't.
    """
    result = {
        'current': None,
        'all_possible': [],
        'currently_assigned': [],
        'note': 'Allowlist every address under all_possible, not just current.',
    }
    # what the outside world actually sees right now
    for url in ('https://api.ipify.org', 'https://ifconfig.me/ip', 'https://icanhazip.com'):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'VoiceGuard/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                ip = r.read().decode().strip()
            if ip:
                result['current'] = ip
                result['checked_with'] = url
                break
        except Exception as e:
            result.setdefault('errors', []).append(f'{url}: {str(e)[:80]}')
    # what Azure itself reports
    assigned = os.getenv('WEBSITE_OUTBOUND_IP_ADDRESSES', '')
    possible = os.getenv('WEBSITE_POSSIBLE_OUTBOUND_IP_ADDRESSES', '')
    result['currently_assigned'] = [x for x in assigned.split(',') if x]
    result['all_possible'] = [x for x in possible.split(',') if x]
    result['site'] = os.getenv('WEBSITE_SITE_NAME', '')
    result['region'] = os.getenv('REGION_NAME', '')
    return jsonify(result)


@app.route('/api/test-db-connection', methods=['GET', 'POST'])
@require_manager
def test_db_connection():
    """Try reaching the phone system's database and report plainly what happened.
    Run this after Igor allowlists the addresses — it separates 'firewall still
    blocking' from 'wrong password' from 'wrong host', which otherwise all look
    the same from the outside.
    """
    # GET so it can simply be opened in the browser while logged in:
    #   /api/test-db-connection?host=example.com&port=443
    d = (request.json or {}) if request.method == 'POST' else {}
    host = (d.get('host') or request.args.get('host') or '').strip()
    try:
        port = int(d.get('port') or request.args.get('port') or 3306)
    except Exception:
        port = 3306
    if not host:
        return jsonify({'error': 'host required'}), 400
    import socket
    out = {'host': host, 'port': port}
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            out['reachable'] = True
            out['ms'] = int((time.time() - t0) * 1000)
            try:
                sock.settimeout(3)
                banner = sock.recv(128)
                out['greeting'] = banner[:60].decode('latin-1', 'replace')
            except Exception:
                out['greeting'] = '(none — normal for some databases)'
        out['meaning'] = 'The port is open to this server. Any failure now is credentials or database name, not the firewall.'
    except socket.timeout:
        out['reachable'] = False
        out['meaning'] = 'Timed out — the firewall is still blocking this server, or the host is wrong.'
    except Exception as e:
        out['reachable'] = False
        out['error'] = str(e)[:160]
        out['meaning'] = 'Could not connect. Check the host name, the port, and that our outbound IPs are allowlisted.'
    return jsonify(out)


@app.route('/api/skinblock/report-detector', methods=['POST'])
def skinblock_report_detector():
    """The page tells us which detector it ended up using and, if it fell back
    to the server, exactly why. Saves asking an agent to read a status line —
    the reason lands in the admin page by itself."""
    d = request.json or {}
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO skinblock_detector_log
                        (agent_ext, mode, reason, backend, isolated, threads, user_agent)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                  (str(d.get('agent_ext') or '')[:8], str(d.get('mode') or '')[:24],
                   str(d.get('reason') or '')[:400], str(d.get('backend') or '')[:24],
                   bool(d.get('isolated')), int(d.get('threads') or 0),
                   str(request.headers.get('User-Agent') or '')[:300]))
        conn.commit(); conn.close()
    except Exception as e:
        print('[skinblock] detector report failed: ' + str(e)[:140])
    return jsonify({'ok': True})


@app.route('/api/skinblock/detector-log', methods=['GET'])
@require_manager
def skinblock_detector_log():
    """Most recent detector result per agent — who is running locally (fast) and
    who is falling back to the server (slow), with the reason."""
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    try:
        c.execute("""SELECT DISTINCT ON (agent_ext)
                        agent_ext, mode, reason, backend, isolated, threads,
                        user_agent, created_at
                     FROM skinblock_detector_log
                     ORDER BY agent_ext, created_at DESC""")
        rows = [dict(r) for r in c.fetchall()]
    except Exception as e:
        conn.close()
        return jsonify({'items': [], 'error': str(e)[:200]})
    conn.close()
    for r in rows:
        r['created_at'] = r['created_at'].isoformat() if r.get('created_at') else None
    return jsonify({'items': rows})


@app.route('/api/skinblock/model-status', methods=['GET'])
def skinblock_model_status():
    """Which detector is live: the trained parser, or the colour fallback."""
    try:
        from skinblock_engine import model_status
        return jsonify(model_status())
    except Exception as e:
        return jsonify({'model': False, 'error': str(e)[:200]})


@app.route('/api/skinblock/process', methods=['POST'])
def skinblock_process():
    """
    Public (extension-gated like submit) — the whole detection pipeline.
    Receives one photo, paints skin/faces server-side, returns the painted PNG.
    All the AI runs here so agents' machines need nothing installed and the
    content filter has nothing to block.
    """
    d = request.json or {}
    s = _skinblock_settings()
    ext = (d.get('agent_ext') or '').strip()
    if s.get('require_extension', True) and not re.fullmatch(r'\d{3}', ext):
        return jsonify({'error': 'A 3-digit extension is required'}), 400
    img_b64 = d.get('image') or ''
    if ',' in img_b64:
        img_b64 = img_b64.split(',', 1)[1]
    try:
        import base64 as _b64
        import numpy as _np
        import cv2 as _cv2
        raw = _b64.b64decode(img_b64)
        arr = _np.frombuffer(raw, _np.uint8)
        img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if img is None:
            return jsonify({'error': 'could not read image'}), 400
        maxd = int(s.get('max_dimension') or 2000)
        hh, ww = img.shape[:2]
        if max(hh, ww) > maxd:
            sc = maxd / float(max(hh, ww))
            img = _cv2.resize(img, (int(ww*sc), int(hh*sc)))
        hexc = (s.get('cover_color') or '#000000').lstrip('#')
        cover = (int(hexc[4:6], 16), int(hexc[2:4], 16), int(hexc[0:2], 16))  # BGR
        import skinblock_engine as _sbe
        # Keep the server engine in step with the browser engine. All of these
        # are Skin Block settings, changeable without a deploy:
        def _num(key, default, lo, hi):
            try:
                return max(lo, min(hi, float(s.get(key, default))))
            except Exception:
                return default
        _sbe.SKIN_BIAS = _num('skin_bias', 0.35, -2.0, 2.0)
        _sbe.SMOOTH_PX = int(_num('smooth_shapes', 0.6, 0.0, 3.0) * 3.3)   # 0.6 -> 2px
        _sbe.COLOUR_TOLERANCE = _num('skin_colour_tolerance', 12, 4, 40)
        _sbe.MAX_GROW_PX = int(_num('skin_reach_px', 90, 8, 300))
        _sbe.COARSE_ENOUGH = _num('coarse_enough', 0.28, 0.05, 1.0)
        _sbe.MAX_GROW_RATIO = _num('skin_grow_limit', 2.4, 1.2, 6.0)
        _sbe.WINDOW_PX = int(_num('window_px', 260, 120, 800))
        _sbe.MAX_WINDOWS = int(_num('max_windows', 60, 1, 200))
        _sbe.TIME_BUDGET = _num('time_budget', 110, 20, 180)
        _sbe.EXTEND_BY_COLOUR = s.get('extend_by_colour', True) is not False
        _sbe.SECOND_PASS = s.get('second_pass', True) is not False
        _sbe.COVER_HAIR = s.get('cover_hair', False) is True
        _sbe.COVER_MODE = 'solid' if str(s.get('cover_mode', 'blend')).lower() == 'solid' else 'blend'
        out, info = _sbe.process(img, cover=cover)
        ok, buf = _cv2.imencode('.png', out)
        if not ok:
            return jsonify({'error': 'encode failed'}), 500
        data_url = 'data:image/png;base64,' + _b64.b64encode(buf.tobytes()).decode()
        return jsonify({'image': data_url, 'info': info})
    except MemoryError:
        print("[skinblock] process failed: out of memory")
        return jsonify({'error': 'server ran out of memory on this image - try a smaller one'}), 500
    except ImportError as e:
        # a missing package is the most likely first-deploy failure, so name it plainly
        print(f"[skinblock] missing dependency: {str(e)[:200]}")
        return jsonify({'error': f'missing package on the server: {str(e)[:120]}'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[skinblock] process failed: {type(e).__name__}: {str(e)[:300]}")
        return jsonify({'error': f'{type(e).__name__}: {str(e)[:200]}'}), 500

@app.route('/api/skinblock/my-recent', methods=['GET'])
def skinblock_my_recent():
    """An agent's own recent photos, so a refresh (or a closed tab) doesn't lose
    the batch they were working through. Scoped to their extension and to the
    last few hours — this is a convenience for the person at the desk, not the
    manager's archive, which lives behind /api/skinblock/history.
    """
    ext = (request.args.get('agent_ext') or '').strip()
    if not re.fullmatch(r'\d{3}', ext):
        return jsonify({'error': 'extension required'}), 400
    try:
        hours = max(1, min(48, int(request.args.get('hours', 12))))
    except Exception:
        hours = 12
    limit = 40
    try:
        _purge_expired_skinblock_images()
    except Exception:
        pass
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("""SELECT id, file_name, person_found, created_at, covered_image, images_purged
                 FROM skinblock_jobs
                 WHERE agent_ext = %s
                   AND created_at >= NOW() - (%s || ' hours')::INTERVAL
                 ORDER BY created_at DESC LIMIT %s""", (ext, str(hours), limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    out = []
    for r in rows:
        if r.get('images_purged') or not r.get('covered_image'):
            continue
        out.append({
            'id': r['id'],
            'file_name': r['file_name'],
            'person_found': bool(r['person_found']),
            'created_at': r['created_at'].isoformat() if r.get('created_at') else '',
            'covered_image': r['covered_image'],
        })
    return jsonify({'items': out, 'hours': hours})


@app.route('/api/skinblock/history', methods=['GET'])
@require_manager
def skinblock_history():
    """Manager view of everything processed, with the images while they're retained."""
    ext = request.args.get('ext', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    limit = min(int(request.args.get('limit', 100)), 500)

    _purge_expired_skinblock_images()

    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    q = '''SELECT id, agent_ext, file_name, person_found, faces_found, manual_bars,
                  coverage_pct, images_purged, created_at,
                  original_image, covered_image
           FROM skinblock_jobs'''
    params, where = [], []
    if ext: where.append('agent_ext = %s'); params.append(ext)
    if date_from: where.append('created_at >= %s'); params.append(date_from)
    if date_to: where.append("created_at <= %s::date + INTERVAL '1 day'"); params.append(date_to)
    if where: q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY created_at DESC LIMIT %s'
    params.append(limit)
    c.execute(q, params)
    jobs = [dict(r) for r in c.fetchall()]

    c.execute('''SELECT agent_ext, COUNT(*) AS total,
                        SUM(CASE WHEN person_found THEN 1 ELSE 0 END) AS with_person
                 FROM skinblock_jobs GROUP BY agent_ext ORDER BY total DESC''')
    by_agent = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'jobs': jobs, 'by_agent': by_agent,
                    'retention_days': _skinblock_settings().get('retention_days', 30)})

def _purge_expired_skinblock_images():
    """Drop stored images past the retention window; the log record itself is kept forever."""
    days = int(_skinblock_settings().get('retention_days', 30))
    try:
        conn = get_db(); c = conn.cursor()
        c.execute(f'''UPDATE skinblock_jobs
                      SET original_image=NULL, covered_image=NULL, images_purged=TRUE
                      WHERE images_purged=FALSE
                        AND created_at < NOW() - INTERVAL '{days} days' ''')
        n = c.rowcount
        conn.commit(); conn.close()
        if n: print(f"[skinblock] purged images for {n} job(s) older than {days} days")
    except Exception as e:
        print(f"[skinblock] purge failed: {str(e)[:120]}")

@app.route('/api/shift-adjustments', methods=['GET'])
@require_manager
def get_shift_adjustments():
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    q, params, where = 'SELECT * FROM shift_adjustments', [], []
    if date_from: where.append('shift_date >= %s'); params.append(date_from)
    if date_to: where.append('shift_date <= %s'); params.append(date_to)
    if where: q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY created_at DESC'
    c.execute(q, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'adjustments': rows})

@app.route('/api/shift-adjustments', methods=['POST'])
@require_manager
def save_shift_adjustment():
    """
    Override the clocked times for one shift. The raw clocker data is never changed —
    this is a separate layer, and the original values are stored alongside so the
    report can always show what was actually logged versus what was adjusted.
    """
    user = current_user()
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    shift_date = d.get('shift_date')
    block_no = int(d.get('block_no') or 1)
    reason = (d.get('reason') or '').strip()
    if not name or not shift_date:
        return jsonify({'error': 'employee_name and shift_date required'}), 400
    if not reason:
        return jsonify({'error': 'A reason is required for any time adjustment'}), 400

    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''INSERT INTO shift_adjustments
        (employee_name, shift_date, block_no, adjusted_in, adjusted_out, adjusted_break_minutes,
         original_in, original_out, original_break_minutes, reason, adjusted_by, adjusted_by_name)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (employee_name, shift_date, block_no) DO UPDATE SET
          adjusted_in=EXCLUDED.adjusted_in,
          adjusted_out=EXCLUDED.adjusted_out,
          adjusted_break_minutes=EXCLUDED.adjusted_break_minutes,
          reason=EXCLUDED.reason,
          adjusted_by=EXCLUDED.adjusted_by,
          adjusted_by_name=EXCLUDED.adjusted_by_name,
          created_at=CURRENT_TIMESTAMP
        RETURNING *''',
        (name, shift_date, block_no,
         d.get('adjusted_in') or None, d.get('adjusted_out') or None,
         d.get('adjusted_break_minutes'),
         d.get('original_in') or None, d.get('original_out') or None,
         d.get('original_break_minutes'),
         reason, user.get('id') if user else None,
         (user.get('full_name') or user.get('username')) if user else 'Unknown'))
    row = dict(c.fetchone()); conn.commit(); conn.close()
    return jsonify(row)

@app.route('/api/shift-adjustments/<int:adj_id>', methods=['DELETE'])
@require_manager
def delete_shift_adjustment(adj_id):
    """Revert to the original clocked times."""
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM shift_adjustments WHERE id=%s', (adj_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/agent-rates', methods=['GET'])
@require_manager
def get_agent_rates():
    """Full rate history, newest first, so the UI can show past and scheduled changes."""
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM agent_rates ORDER BY employee_name, effective_from DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'rates': rows})

@app.route('/api/agent-rates', methods=['POST'])
@require_manager
def save_agent_rate():
    """
    Add a rate for an agent, effective from a given date/time.
    Past shifts keep whatever rate was in force when they were worked.
    """
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    rate = d.get('hourly_rate')
    eff = d.get('effective_from') or '2000-01-01T00:00:00'
    if not name or rate is None:
        return jsonify({'error': 'employee_name and hourly_rate required'}), 400
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    # Replace any rate with the exact same effective time, else append a new one
    c.execute('DELETE FROM agent_rates WHERE employee_name=%s AND effective_from=%s', (name, eff))
    c.execute('''INSERT INTO agent_rates (employee_name, hourly_rate, effective_from, note)
                 VALUES (%s,%s,%s,%s) RETURNING *''',
              (name, float(rate), eff, d.get('note','')))
    row = dict(c.fetchone()); conn.commit(); conn.close()
    return jsonify(row)

@app.route('/api/agent-rates/<int:rate_id>', methods=['DELETE'])
@require_manager
def delete_agent_rate(rate_id):
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM agent_rates WHERE id=%s', (rate_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/ot-periods', methods=['GET'])
@require_manager
def get_ot_periods():
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM ot_multiplier_periods ORDER BY starts_at DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'periods': rows, 'default_multiplier': DEFAULT_OT_MULTIPLIER})

@app.route('/api/ot-periods', methods=['POST'])
@require_manager
def create_ot_period():
    """Declare a special overtime rate. ends_at null = 'until further notice'."""
    user = current_user()
    d = request.json or {}
    starts_at = d.get('starts_at')
    if not starts_at:
        return jsonify({'error': 'starts_at required'}), 400
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''INSERT INTO ot_multiplier_periods (starts_at, ends_at, multiplier, note, created_by)
                 VALUES (%s,%s,%s,%s,%s) RETURNING *''',
              (starts_at, d.get('ends_at') or None, float(d.get('multiplier') or 2.0),
               d.get('note',''), user.get('id') if user else None))
    row = dict(c.fetchone()); conn.commit(); conn.close()
    return jsonify(row)

@app.route('/api/ot-periods/<int:pid>/end', methods=['POST'])
@require_manager
def end_ot_period(pid):
    """Close an open-ended special rate ('further notice' has arrived)."""
    d = request.json or {}
    ends_at = d.get('ends_at') or datetime.now().isoformat(timespec='seconds')
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('UPDATE ot_multiplier_periods SET ends_at=%s WHERE id=%s RETURNING *', (ends_at, pid))
    row = c.fetchone()
    conn.commit(); conn.close()
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))

@app.route('/api/ot-periods/<int:pid>', methods=['DELETE'])
@require_manager
def delete_ot_period(pid):
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM ot_multiplier_periods WHERE id=%s', (pid,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/recurring-schedules', methods=['GET'])
@require_manager
def get_recurring_schedules():
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM recurring_schedules ORDER BY employee_name, day_of_week')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'recurring': rows})

@app.route('/api/recurring-schedules', methods=['POST'])
@require_manager
def save_recurring_schedule():
    """
    Save one weekday of an agent's permanent weekly schedule.
    Body: employee_name, day_of_week (0=Mon..6=Sun), scheduled_in_time 'HH:MM',
          scheduled_out_time 'HH:MM', overnight (bool), break_minutes.
    Pass active=false to mark that weekday as a day off.
    """
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    dow = d.get('day_of_week')
    in_t = d.get('scheduled_in_time')
    out_t = d.get('scheduled_out_time')
    if not name or dow is None or not in_t or not out_t:
        return jsonify({'error': 'employee_name, day_of_week, scheduled_in_time, scheduled_out_time required'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''INSERT INTO recurring_schedules
                 (employee_name, day_of_week, scheduled_in_time, scheduled_out_time, overnight, break_minutes, active)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (employee_name, day_of_week) DO UPDATE SET
                   scheduled_in_time=EXCLUDED.scheduled_in_time,
                   scheduled_out_time=EXCLUDED.scheduled_out_time,
                   overnight=EXCLUDED.overnight,
                   break_minutes=EXCLUDED.break_minutes,
                   active=EXCLUDED.active
                 RETURNING *''',
              (name, int(dow), in_t, out_t, bool(d.get('overnight', False)),
               int(d.get('break_minutes') or 0), bool(d.get('active', True))))
    row = dict(c.fetchone())
    conn.commit(); conn.close()
    return jsonify(row)

@app.route('/api/recurring-schedules/bulk', methods=['POST'])
@require_manager
def save_recurring_bulk():
    """
    Save a full week for one agent in a single call.
    Body: {employee_name, days: [{day_of_week, scheduled_in_time, scheduled_out_time,
           overnight, break_minutes, active}]}
    """
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    days = d.get('days', [])
    if not name or not days:
        return jsonify({'error': 'employee_name and days required'}), 400
    conn = get_db(); c = conn.cursor()
    # Replace this agent's whole weekly pattern so removed blocks disappear
    c.execute('DELETE FROM recurring_schedules WHERE employee_name=%s', (name,))
    for day in days:
        c.execute('''INSERT INTO recurring_schedules
                     (employee_name, day_of_week, block_no, scheduled_in_time, scheduled_out_time, overnight, break_minutes, active)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                  (name, int(day.get('day_of_week')), int(day.get('block_no') or 1),
                   day.get('scheduled_in_time','09:00'),
                   day.get('scheduled_out_time','17:00'), bool(day.get('overnight', False)),
                   int(day.get('break_minutes') or 0), bool(day.get('active', True))))
    conn.commit(); conn.close()
    return jsonify({'success': True, 'saved': len(days)})

@app.route('/api/recurring-schedules/<int:rec_id>', methods=['DELETE'])
@require_manager
def delete_recurring_schedule(rec_id):
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM recurring_schedules WHERE id=%s', (rec_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

def _resolve_schedules(date_from, date_to, employee=''):
    """
    Builds the effective schedule list for a date range.
    A specific agent_schedules row for a date ALWAYS wins (an override for that day);
    otherwise the agent's recurring weekly pattern generates the schedule for that date.
    Returns a list of dicts shaped like agent_schedules rows.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # Specific per-date overrides
    sq = 'SELECT * FROM agent_schedules'
    sparams, swhere = [], []
    if date_from: swhere.append('shift_date >= %s'); sparams.append(date_from)
    if date_to: swhere.append('shift_date <= %s'); sparams.append(date_to)
    if employee: swhere.append('employee_name = %s'); sparams.append(employee)
    if swhere: sq += ' WHERE ' + ' AND '.join(swhere)
    c.execute(sq, sparams)
    specific = [dict(r) for r in c.fetchall()]
    override_keys = {(s['employee_name'], str(s['shift_date']), s.get('block_no') or 1) for s in specific}

    # Per-week schedules (a specific week's own times — these beat the default pattern)
    wq = 'SELECT * FROM week_schedules'
    wparams, wwhere = [], []
    if employee: wwhere.append('employee_name = %s'); wparams.append(employee)
    if wwhere: wq += ' WHERE ' + ' AND '.join(wwhere)
    c.execute(wq, wparams)
    weekrows = [dict(r) for r in c.fetchall()]
    # employee+week that have ANY row are fully defined by those rows, so a day
    # left off in a customised week correctly means "off" instead of falling back
    week_defined = {(w['employee_name'], str(w['week_start'])) for w in weekrows}
    by_week = {}
    for w in weekrows:
        if w.get('active') is False:
            continue
        by_week.setdefault((w['employee_name'], str(w['week_start']), w['day_of_week']), []).append(w)

    # Recurring weekly patterns (the default, used for any week not customised)
    rq = 'SELECT * FROM recurring_schedules WHERE active = TRUE'
    rparams = []
    if employee:
        rq += ' AND employee_name = %s'; rparams.append(employee)
    c.execute(rq, rparams)
    recurring = [dict(r) for r in c.fetchall()]
    conn.close()

    generated = []
    if recurring and date_from and date_to:
        start = _dt.strptime(date_from, '%Y-%m-%d').date()
        end = _dt.strptime(date_to, '%Y-%m-%d').date()
        by_dow = {}
        for r in recurring:
            by_dow.setdefault(r['day_of_week'], []).append(r)

        by_dow_seen = set()
        cur = start
        while cur <= end:
            dow = cur.weekday()  # Monday=0 .. Sunday=6
            wk = str(cur - _td(days=dow))          # Monday of this date's week
            for r in by_dow.get(dow, []):
                emp = r['employee_name']
                # if this agent's week was customised, the week's own rows are the truth
                if (emp, wk) in week_defined:
                    for wrow in by_week.get((emp, wk, dow), []):
                        wblk = wrow.get('block_no') or 1
                        if (emp, str(cur), wblk) in override_keys:
                            continue
                        wi_h, wi_m = [int(x) for x in str(wrow['scheduled_in_time']).split(':')[:2]]
                        wo_h, wo_m = [int(x) for x in str(wrow['scheduled_out_time']).split(':')[:2]]
                        w_in = _dt.combine(cur, _dt.min.time()).replace(hour=wi_h, minute=wi_m)
                        w_out_date = cur + _td(days=1) if wrow['overnight'] else cur
                        w_out = _dt.combine(w_out_date, _dt.min.time()).replace(hour=wo_h, minute=wo_m)
                        generated.append({
                            'id': None,
                            'employee_name': emp,
                            'shift_date': cur,
                            'block_no': wblk,
                            'scheduled_in': w_in,
                            'scheduled_out': w_out,
                            'break_minutes': wrow['break_minutes'] or 0,
                            'from_recurring': True,
                            'from_week': True,
                        })
                    by_dow_seen.add((emp, wk, dow))
                    continue
                blk = r.get('block_no') or 1
                if (r['employee_name'], str(cur), blk) in override_keys:
                    continue  # a specific entry for that date+block wins
                in_h, in_m = [int(x) for x in str(r['scheduled_in_time']).split(':')[:2]]
                out_h, out_m = [int(x) for x in str(r['scheduled_out_time']).split(':')[:2]]
                sched_in = _dt.combine(cur, _dt.min.time()).replace(hour=in_h, minute=in_m)
                out_date = cur + _td(days=1) if r['overnight'] else cur
                sched_out = _dt.combine(out_date, _dt.min.time()).replace(hour=out_h, minute=out_m)
                generated.append({
                    'id': None,
                    'employee_name': r['employee_name'],
                    'shift_date': cur,
                    'block_no': blk,
                    'scheduled_in': sched_in,
                    'scheduled_out': sched_out,
                    'break_minutes': r['break_minutes'] or 0,
                    'from_recurring': True,
                })
            # agents whose week was customised but who have no default pattern row
            for (emp, wkey, wdow), wlist in by_week.items():
                if wkey != wk or wdow != dow or (emp, wk, dow) in by_dow_seen:
                    continue
                for wrow in wlist:
                    wblk = wrow.get('block_no') or 1
                    if (emp, str(cur), wblk) in override_keys:
                        continue
                    wi_h, wi_m = [int(x) for x in str(wrow['scheduled_in_time']).split(':')[:2]]
                    wo_h, wo_m = [int(x) for x in str(wrow['scheduled_out_time']).split(':')[:2]]
                    w_in = _dt.combine(cur, _dt.min.time()).replace(hour=wi_h, minute=wi_m)
                    w_out_date = cur + _td(days=1) if wrow['overnight'] else cur
                    w_out = _dt.combine(w_out_date, _dt.min.time()).replace(hour=wo_h, minute=wo_m)
                    generated.append({
                        'id': None, 'employee_name': emp, 'shift_date': cur,
                        'block_no': wblk, 'scheduled_in': w_in, 'scheduled_out': w_out,
                        'break_minutes': wrow['break_minutes'] or 0,
                        'from_recurring': True, 'from_week': True,
                    })
            cur += _td(days=1)

    for s in specific:
        s['from_recurring'] = False
    return specific + generated

def _week_start(d):
    """Monday of the week containing date-string d (YYYY-MM-DD)."""
    from datetime import datetime as _dt, timedelta as _td
    dt = _dt.strptime(d, '%Y-%m-%d').date()
    return dt - _td(days=dt.weekday())


@app.route('/api/week-schedules', methods=['GET'])
@require_manager
def get_week_schedules():
    """
    Schedules for one specific week. Returns the week's own rows plus the default
    weekly pattern, and which agents have had this week customised — so the UI can
    show 'this week' accurately without the caller doing the merge.
    """
    ws = request.args.get('week_start', '')
    if not ws:
        return jsonify({'error': 'week_start required'}), 400
    try:
        ws = str(_week_start(ws))
    except Exception:
        return jsonify({'error': 'bad week_start'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM week_schedules WHERE week_start=%s ORDER BY employee_name, day_of_week, block_no', (ws,))
    week = [dict(r) for r in c.fetchall()]
    c.execute('SELECT * FROM recurring_schedules WHERE active=TRUE ORDER BY employee_name, day_of_week, block_no')
    default = [dict(r) for r in c.fetchall()]
    conn.close()
    customised = sorted({w['employee_name'] for w in week})
    return jsonify({'week_start': ws, 'week': week, 'default': default, 'customised': customised})


@app.route('/api/week-schedules/bulk', methods=['POST'])
@require_manager
def save_week_schedule():
    """Replace one agent's schedule for one week. Past weeks are untouched."""
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    ws = (d.get('week_start') or '').strip()
    days = d.get('days') or []
    if not name or not ws:
        return jsonify({'error': 'employee_name and week_start required'}), 400
    try:
        ws = str(_week_start(ws))
    except Exception:
        return jsonify({'error': 'bad week_start'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM week_schedules WHERE employee_name=%s AND week_start=%s', (name, ws))
    saved = 0
    for day in days:
        if not day.get('active', True):
            continue
        c.execute("""INSERT INTO week_schedules
            (employee_name, week_start, day_of_week, scheduled_in_time, scheduled_out_time,
             overnight, break_minutes, active, block_no)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)""",
            (name, ws, int(day.get('day_of_week', 0)),
             day.get('scheduled_in_time', '09:00'), day.get('scheduled_out_time', '17:00'),
             bool(day.get('overnight')), int(day.get('break_minutes') or 0),
             int(day.get('block_no') or 1)))
        saved += 1
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'saved': saved, 'week_start': ws})


@app.route('/api/week-schedules/copy', methods=['POST'])
@require_manager
def copy_week_schedule():
    """
    Fill a week from the previous week (or any chosen source week). Agents already
    customised in the target week are skipped unless overwrite is true. If the source
    week has no rows of its own for an agent, that agent's default pattern is copied,
    so the first use still produces a full, editable week.
    """
    d = request.json or {}
    target = (d.get('week_start') or '').strip()
    source = (d.get('from_week_start') or '').strip()
    overwrite = bool(d.get('overwrite'))
    if not target:
        return jsonify({'error': 'week_start required'}), 400
    from datetime import timedelta as _td
    try:
        tgt = _week_start(target)
        src = _week_start(source) if source else (tgt - _td(days=7))
    except Exception:
        return jsonify({'error': 'bad week'}), 400
    if str(tgt) == str(src):
        return jsonify({'error': 'source and target are the same week'}), 400

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT * FROM week_schedules WHERE week_start=%s', (str(src),))
    src_rows = [dict(r) for r in c.fetchall()]
    c.execute('SELECT employee_name FROM week_schedules WHERE week_start=%s', (str(tgt),))
    already = {r['employee_name'] for r in c.fetchall()}
    c.execute('SELECT * FROM recurring_schedules WHERE active=TRUE')
    default_rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # Per agent: prefer the source week's own rows, else that agent's whole
    # default pattern. Group both sides FIRST — checking "is this agent already
    # present" row by row would stop after their first day and copy a single
    # weekday per agent.
    src_by_agent, def_by_agent = {}, {}
    for r in src_rows:
        src_by_agent.setdefault(r['employee_name'], []).append(r)
    for r in default_rows:
        def_by_agent.setdefault(r['employee_name'], []).append(r)
    by_agent = {}
    for name in set(src_by_agent) | set(def_by_agent):
        by_agent[name] = src_by_agent.get(name) or def_by_agent.get(name, [])

    conn = get_db()
    c = conn.cursor()
    agents, rows = 0, 0
    for name, items in by_agent.items():
        if name in already and not overwrite:
            continue
        c.execute('DELETE FROM week_schedules WHERE employee_name=%s AND week_start=%s', (name, str(tgt)))
        for r in items:
            if r.get('active') is False:
                continue
            c.execute("""INSERT INTO week_schedules
                (employee_name, week_start, day_of_week, scheduled_in_time, scheduled_out_time,
                 overnight, break_minutes, active, block_no)
                VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
                ON CONFLICT (employee_name, week_start, day_of_week, block_no) DO NOTHING""",
                (name, str(tgt), r['day_of_week'], r['scheduled_in_time'], r['scheduled_out_time'],
                 bool(r['overnight']), int(r['break_minutes'] or 0), int(r.get('block_no') or 1)))
            rows += 1
        agents += 1
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'agents': agents, 'rows': rows,
                    'from_week_start': str(src), 'week_start': str(tgt),
                    'skipped': sorted(already) if not overwrite else []})


@app.route('/api/week-schedules', methods=['DELETE'])
@require_manager
def delete_week_schedule():
    """Drop a week's custom rows so that week falls back to the default pattern."""
    ws = request.args.get('week_start', '')
    emp = request.args.get('employee', '')
    if not ws:
        return jsonify({'error': 'week_start required'}), 400
    try:
        ws = str(_week_start(ws))
    except Exception:
        return jsonify({'error': 'bad week_start'}), 400
    conn = get_db()
    c = conn.cursor()
    if emp:
        c.execute('DELETE FROM week_schedules WHERE week_start=%s AND employee_name=%s', (ws, emp))
    else:
        c.execute('DELETE FROM week_schedules WHERE week_start=%s', (ws,))
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'deleted': n})


@app.route('/api/schedules', methods=['GET'])
@require_manager
def get_schedules():
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    q = 'SELECT * FROM agent_schedules'
    params, where = [], []
    if date_from: where.append('shift_date >= %s'); params.append(date_from)
    if date_to: where.append('shift_date <= %s'); params.append(date_to)
    if where: q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY shift_date DESC, employee_name ASC'
    c.execute(q, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'schedules': rows})

@app.route('/api/schedules', methods=['POST'])
@require_manager
def save_schedule():
    """Create or update a scheduled shift. Body: employee_name, shift_date,
    scheduled_in (ISO), scheduled_out (ISO), break_minutes."""
    d = request.json or {}
    name = (d.get('employee_name') or '').strip()
    shift_date = d.get('shift_date')
    sched_in = d.get('scheduled_in')
    sched_out = d.get('scheduled_out')
    break_min = int(d.get('break_minutes') or 0)
    if not (name and shift_date and sched_in and sched_out):
        return jsonify({'error': 'employee_name, shift_date, scheduled_in and scheduled_out are required'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    block_no = int(d.get('block_no') or 1)
    c.execute('''INSERT INTO agent_schedules (employee_name, shift_date, block_no, scheduled_in, scheduled_out, break_minutes, notes)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (employee_name, shift_date, block_no) DO UPDATE SET
                   scheduled_in=EXCLUDED.scheduled_in, scheduled_out=EXCLUDED.scheduled_out,
                   break_minutes=EXCLUDED.break_minutes, notes=EXCLUDED.notes
                 RETURNING *''',
              (name, shift_date, block_no, sched_in, sched_out, break_min, d.get('notes','')))
    row = dict(c.fetchone())
    conn.commit(); conn.close()
    return jsonify(row)

@app.route('/api/schedules/<int:sched_id>', methods=['DELETE'])
@require_manager
def delete_schedule(sched_id):
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM agent_schedules WHERE id=%s', (sched_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/clocker-upload', methods=['POST'])
@require_manager
def clocker_upload():
    """
    Accepts parsed clocker rows from the CMS export.
    Body: {events: [{employee_name, event_time (ISO), status, break_minutes, break_reason}]}
    Duplicates are ignored so the same report can be re-uploaded safely.
    """
    d = request.json or {}
    events = d.get('events', [])
    if not events:
        return jsonify({'error': 'No events provided'}), 400
    conn = get_db(); c = conn.cursor()
    inserted = 0
    for e in events:
        try:
            c.execute('''INSERT INTO clocker_events (employee_name, event_time, status, break_minutes, break_reason)
                         VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (employee_name, event_time, status) DO NOTHING''',
                      (e.get('employee_name'), e.get('event_time'), e.get('status'),
                       e.get('break_minutes'), e.get('break_reason')))
            inserted += c.rowcount
        except Exception as ex:
            print(f"[Clocker] Skipped row: {ex}")
    conn.commit(); conn.close()
    return jsonify({'success': True, 'received': len(events), 'inserted': inserted})

@app.route('/api/cms-db/test', methods=['GET'])
@require_manager
def cms_db_test():
    """Can VoiceGuard reach the CMS database, and is the login accepted."""
    import cms_db
    return jsonify(cms_db.test())


@app.route('/api/cms-db/databases', methods=['GET'])
@require_manager
def cms_db_databases():
    """Every database on the CMS server."""
    import cms_db
    try:
        return jsonify(cms_db.databases())
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/diagnose', methods=['GET'])
@require_manager
def cms_db_diagnose():
    """What the login actually is and what it can see — for when a search comes
    back with nothing and it isn't obvious whether that's permissions or not."""
    import cms_db
    try:
        return jsonify(cms_db.diagnose())
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/tables', methods=['GET'])
@require_manager
def cms_db_tables():
    """Tables with row counts, biggest first — and the likely clock-event ones
    flagged. Works on any database on the server, not just the configured one."""
    import cms_db
    db = request.args.get('database') or None
    try:
        out = cms_db.tables_in(db)
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400
    hints = ('clock', 'attend', 'shift', 'agent', 'employee', 'login', 'session',
             'timesheet', 'status', 'break', 'staff', 'user')
    out['likely'] = [t['name'] for t in out['tables']
                     if any(h in t['name'].lower() for h in hints)]
    return jsonify(out)


@app.route('/api/cms-db/search', methods=['GET'])
@require_manager
def cms_db_search():
    """Find any table or column whose name contains a word — 'where is X kept?'"""
    import cms_db
    try:
        return jsonify(cms_db.search(request.args.get('q', ''),
                                     request.args.get('database') or None))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


NOTE_STOPWORDS = set("""
a an the and or but if then than so because as of to in on at for with from by about into over after
before between out against during without within along across behind beyond up down off above below
is are was were be been being am do does did doing have has had having will would shall should may
might must can could i me my we our you your he him his she her it its they them their this that
these those there here what which who whom when where why how all any both each few more most other
some such no nor not only own same s t just dont should now got get getting im ive hes shes theyre
said says say told tell asked ask call called calls caller customer client
""".split())


def _note_words(text):
    import re
    return [w for w in re.findall(r"[a-z']{2,}", (text or '').lower())]


def _notes_char_budget():
    """How much note text one question may read. Bigger reads mean more notes
    considered and a higher cost per question."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'notes_char_budget'")
        r = c.fetchone(); conn.close()
        if r and r[0]:
            return max(20_000, min(750_000, int(float(r[0]))))
    except Exception:
        pass
    return NOTES_CHAR_BUDGET_DEFAULT


def _call_notes_filters(a):
    """Shared filter for the notes browser and the question box, so an answer is
    always about exactly the notes on screen."""
    where, params = ["COALESCE(call_notes,'') <> ''"], []
    if a.get('date_from'):
        where.append('created_at >= %s'); params.append(a['date_from'] + ' 00:00:00')
    if a.get('date_to'):
        where.append('created_at <= %s'); params.append(a['date_to'] + ' 23:59:59')
    if a.get('agent'):
        where.append('agent_name = %s'); params.append(a['agent'])
    if a.get('phone'):
        where.append('caller_id LIKE %s'); params.append('%' + a['phone'] + '%')
    if a.get('account'):
        where.append('(account_name LIKE %s OR customer_account_id LIKE %s)')
        params += ['%' + a['account'] + '%'] * 2
    if a.get('contains'):
        where.append('call_notes LIKE %s'); params.append('%' + a['contains'] + '%')
    # Notes exist whether or not the AI ever scored the call — the record is
    # saved the moment it arrives. This lets you look at just the ones the AI
    # never got to (paused, failed, still queued), which are invisible on the
    # quality pages precisely because they were never scored.
    st = (a.get('processed') or '').lower()
    if st == 'yes':
        where.append("COALESCE(status,'') NOT IN ('Paused','Failed','Pending','Processing')")
    elif st == 'no':
        where.append("COALESCE(status,'') IN ('Paused','Failed','Pending','Processing')")
    return ' AND '.join(where), params


@app.route('/api/call-notes', methods=['GET'])
@require_manager
def call_notes_list():
    """Every call note, filterable by agent, date, phone number and account."""
    a = {k: (request.args.get(k) or '').strip() for k in
         ('date_from', 'date_to', 'agent', 'phone', 'account', 'contains', 'processed')}
    try:
        limit = min(2000, max(1, int(request.args.get('limit') or 300)))
        offset = max(0, int(request.args.get('offset') or 0))
    except Exception:
        limit, offset = 300, 0
    where, params = _call_notes_filters(a)
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT COUNT(*) AS n FROM calls WHERE ' + where, params)
    total = c.fetchone()['n']
    c.execute("""SELECT COUNT(*) AS n FROM calls WHERE %s
                 AND COALESCE(status,'') IN ('Paused','Failed','Pending','Processing')""" % where,
              params)
    not_analyzed = c.fetchone()['n']
    c.execute("""SELECT call_id, agent_name, agent_extension, caller_id, account_name,
                        customer_account_id, duration, created_at, call_notes, notes_score, status
                 FROM calls WHERE %s ORDER BY created_at DESC LIMIT %s OFFSET %s"""
              % (where, limit, offset), params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        if r.get('created_at'):
            r['created_at'] = r['created_at'].isoformat()
    return jsonify({'notes': rows, 'total': total, 'not_analyzed': not_analyzed,
                    'limit': limit, 'offset': offset})


DEFAULT_NOTE_TOPICS = {
    'refund / money back': ['refund', 'money back', 'chargeback', 'credit back', 'reimburse'],
    'cancel / leaving':    ['cancel', 'cancelled', 'canceling', 'close account', 'stop service', 'switch to'],
    'complaint / upset':   ['complain', 'complaint', 'upset', 'angry', 'furious', 'frustrated', 'unhappy', 'rude'],
    'billing question':    ['bill', 'billed', 'invoice', 'charge', 'overcharge', 'payment', 'price'],
    'delivery / shipping': ['deliver', 'delivery', 'shipping', 'shipment', 'tracking', 'package', 'arrive'],
    'broken / damaged':    ['broken', 'damaged', 'defect', 'not working', 'stopped working', 'faulty'],
    'late / waiting':      ['late', 'delay', 'delayed', 'still waiting', 'never received', 'no response'],
    'callback promised':   ['call back', 'callback', 'will call', 'follow up', 'get back to'],
    'escalation':          ['manager', 'supervisor', 'escalate', 'escalated', 'complaint filed'],
    'new order / sale':    ['ordered', 'new order', 'placed order', 'purchase', 'signed up', 'upgrade'],
}


@app.route('/api/call-notes/insights', methods=['GET'])
@require_manager
def call_notes_insights():
    """Analysis of the filtered notes without any AI — free, and it reads every
    matching note however many there are.

    Counting, grouping and word frequency answer a surprising amount on their
    own: what gets mentioned, which accounts come up most, how the volume moves
    over time, and which notes look like nothing was written. Claude is for the
    questions that need reading comprehension; this is for the rest.
    """
    import re
    from collections import Counter

    a = {k: (request.args.get(k) or '').strip() for k in
         ('date_from', 'date_to', 'agent', 'phone', 'account', 'contains', 'processed')}
    where, params = _call_notes_filters(a)

    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)

    # --- things the database can count on its own, at any scale -------------
    c.execute("""SELECT COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                        AVG(LENGTH(call_notes))::numeric(10,1) AS avg_len
                 FROM calls WHERE """ + where, params)
    head = dict(c.fetchone())

    c.execute("""SELECT DATE(created_at) AS day, COUNT(*) AS n
                 FROM calls WHERE %s GROUP BY DATE(created_at)
                 ORDER BY DATE(created_at)""" % where, params)
    by_day = [{'day': str(r['day']), 'n': r['n']} for r in c.fetchall()]

    c.execute("""SELECT COALESCE(NULLIF(agent_name,''),'(unknown)') AS k, COUNT(*) AS n,
                        AVG(LENGTH(call_notes))::numeric(10,1) AS avg_len
                 FROM calls WHERE %s GROUP BY 1 ORDER BY n DESC LIMIT 50""" % where, params)
    by_agent = [dict(r) for r in c.fetchall()]

    c.execute("""SELECT COALESCE(NULLIF(account_name,''), NULLIF(customer_account_id,''),'(none)') AS k,
                        COUNT(*) AS n
                 FROM calls WHERE %s GROUP BY 1 ORDER BY n DESC LIMIT 25""" % where, params)
    by_account = [dict(r) for r in c.fetchall()]

    c.execute("""SELECT COALESCE(NULLIF(caller_id,''),'(none)') AS k, COUNT(*) AS n
                 FROM calls WHERE %s GROUP BY 1 HAVING COUNT(*) > 1
                 ORDER BY n DESC LIMIT 25""" % where, params)
    repeat_callers = [dict(r) for r in c.fetchall()]

    # --- topics: counted in the database, so every note is included ---------
    topics = []
    for label, words in DEFAULT_NOTE_TOPICS.items():
        clause = ' OR '.join(['LOWER(call_notes) LIKE %s'] * len(words))
        c.execute('SELECT COUNT(*) AS n FROM calls WHERE (%s) AND (%s)' % (where, clause),
                  params + ['%' + w + '%' for w in words])
        n = c.fetchone()['n']
        if n:
            topics.append({'topic': label, 'notes': n,
                           'pct': round(n / max(1, head['n']) * 100, 1)})
    topics.sort(key=lambda t: -t['notes'])

    # --- word and phrase counts: streamed in batches, no cap ---------------
    words = Counter(); phrases = Counter(); exact = Counter()
    scanned, batch, offset = 0, 5000, 0
    while True:
        c.execute("""SELECT call_notes FROM calls WHERE %s
                     ORDER BY created_at DESC LIMIT %d OFFSET %d""" % (where, batch, offset), params)
        chunk = c.fetchall()
        if not chunk:
            break
        for r in chunk:
            t = (r['call_notes'] or '').strip()
            if not t:
                continue
            scanned += 1
            key = re.sub(r'\s+', ' ', t.lower())
            if len(key) >= 5:
                exact[key] += 1
            ws = re.findall(r"[a-z']{2,}", t.lower())
            words.update(w for w in ws if w not in NOTE_STOPWORDS)
            for i in range(len(ws) - 1):
                if ws[i] in NOTE_STOPWORDS and ws[i+1] in NOTE_STOPWORDS:
                    continue
                phrases[' '.join(ws[i:i+2])] += 1
        offset += batch
        if len(chunk) < batch:
            break
    conn.close()

    repeated = [{'text': t[:160], 'times': n} for t, n in exact.most_common(20) if n > 1]
    return jsonify({
        'total_notes': head['n'],
        'scanned': scanned,
        'first_at': head['first_at'].isoformat() if head.get('first_at') else None,
        'last_at': head['last_at'].isoformat() if head.get('last_at') else None,
        'avg_length': float(head['avg_len'] or 0),
        'by_day': by_day,
        'by_agent': [{'agent': r['k'], 'notes': r['n'], 'avg_length': float(r['avg_len'] or 0)}
                     for r in by_agent],
        'by_account': [{'account': r['k'], 'notes': r['n']} for r in by_account],
        'repeat_callers': [{'phone': r['k'], 'calls': r['n']} for r in repeat_callers],
        'topics': topics,
        'top_words': words.most_common(40),
        'top_phrases': phrases.most_common(30),
        'repeated_notes': repeated,
        'free': True,
    })


@app.route('/api/call-notes/estimate', methods=['GET'])
@require_manager
def call_notes_estimate():
    """Roughly what the next question will cost, before it's asked. Based on the
    size of the notes that match the current filters — about 4 characters per
    token, which is close enough for a price shown to two decimal places."""
    a = {k: (request.args.get(k) or '').strip() for k in
         ('date_from', 'date_to', 'agent', 'phone', 'account', 'contains', 'processed')}
    where, params = _call_notes_filters(a)
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM calls WHERE ' + where, params)
    total = int(c.fetchone()[0] or 0)
    budget = _notes_char_budget()
    c.execute("""SELECT COALESCE(SUM(LENGTH(call_notes)), 0), COUNT(*) FROM (
                     SELECT call_notes FROM calls WHERE %s
                     ORDER BY created_at DESC LIMIT %d) t""" % (where, MAX_NOTES_HARD), params)
    row = c.fetchone()
    all_chars, fetched_n = int(row[0] or 0), int(row[1] or 0)
    chars = min(all_chars, budget)
    # how many notes that budget actually covers
    notes_read = fetched_n if all_chars <= budget else max(1, int(fetched_n * budget / max(1, all_chars)))
    c.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage
                 WHERE service = 'claude-notes-question' AND used_at >= CURRENT_DATE""")
    spent_today = float(c.fetchone()[0] or 0)
    conn.close()

    in_tok = chars / 4 + 400          # the notes, plus the question and instructions
    out_tok = 500                     # a typical answer
    cost = in_tok / 1_000_000 * CLAUDE_INPUT_COST_PER_M + out_tok / 1_000_000 * CLAUDE_OUTPUT_COST_PER_M
    return jsonify({'estimate_usd': round(cost, 4), 'notes_matching': total,
                    'notes_read': min(total, notes_read),
                    'char_budget': budget,
                    'spent_today_usd': round(spent_today, 4)})


@app.route('/api/call-notes/ask', methods=['POST'])
@require_manager
def call_notes_ask():
    """Ask a question about the notes currently filtered.

    Only the notes matching the filters are sent, so the answer is grounded in
    what's on screen rather than the whole database. If there are more notes
    than fit in one question, the most recent are used and that is stated.
    """
    d = request.json or {}
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'Type a question first'}), 400
    a = {k: (d.get(k) or '').strip() for k in
         ('date_from', 'date_to', 'agent', 'phone', 'account', 'contains', 'processed')}
    where, params = _call_notes_filters(a)

    # How many notes fit is a question of SIZE, not a round number of rows.
    # Claude reads a 200,000-token window; at roughly 4 characters per token
    # that's a lot of notes — thousands of short ones. So take notes until the
    # character budget is used up rather than stopping at an arbitrary count.
    # The ceiling is a setting, because a bigger read costs more per question.
    budget_chars = _notes_char_budget()
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT COUNT(*) AS n FROM calls WHERE ' + where, params)
    total = c.fetchone()['n']
    c.execute("""SELECT agent_name, caller_id, account_name, created_at, call_notes
                 FROM calls WHERE %s ORDER BY created_at DESC LIMIT %s"""
              % (where, MAX_NOTES_HARD), params)
    fetched = [dict(r) for r in c.fetchall()]
    rows, used_chars = [], 0
    for r in fetched:
        n = len(r.get('call_notes') or '') + 60      # the date/agent/account prefix
        if rows and used_chars + n > budget_chars:
            break
        rows.append(r); used_chars += n
    conn.close()
    if not rows:
        return jsonify({'answer': 'No notes match these filters, so there is nothing to read.',
                        'notes_used': 0, 'total_matching': 0})

    lines = []
    for i, r in enumerate(rows, 1):
        when = r['created_at'].strftime('%Y-%m-%d %H:%M') if r.get('created_at') else ''
        lines.append('%d. [%s | %s | %s] %s' % (
            i, when, r.get('agent_name') or '?', r.get('account_name') or '', 
            (r.get('call_notes') or '').replace('\n', ' ')[:600]))
    corpus = '\n'.join(lines)

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'No Anthropic API key is set on the server '
                                 '(ANTHROPIC_API_KEY). The free analysis above still works.'}), 400
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""These are notes call-center agents wrote after phone calls. Each line is:
number. [date time | agent | account] the note text.

NOTES ({len(rows)} of {total} matching):
{corpus}

QUESTION: {question}

Answer from these notes only. Be specific and quote or cite note numbers where it helps.
If the notes don't contain the answer, say so plainly rather than guessing. Keep it brief
and practical — the reader runs this call center."""
        resp = client.messages.create(model='claude-sonnet-4-6', max_tokens=1200,
                                      messages=[{'role': 'user', 'content': prompt}])
        answer = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
        in_tok = getattr(resp.usage, 'input_tokens', 0) or 0
        out_tok = getattr(resp.usage, 'output_tokens', 0) or 0
    except Exception as e:
        return jsonify({'error': 'Could not reach Claude: ' + str(e)[:200]}), 502

    # Same rates the call scoring uses, so this shows up in the cost page
    # alongside everything else rather than being an invisible extra.
    cost = (in_tok / 1_000_000 * CLAUDE_INPUT_COST_PER_M
            + out_tok / 1_000_000 * CLAUDE_OUTPUT_COST_PER_M)
    spent_today = 0.0
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO api_usage (service, call_id, input_tokens, output_tokens, cost_usd)
                     VALUES ('claude-notes-question', NULL, %s, %s, %s)""",
                  (in_tok, out_tok, round(cost, 6)))
        conn.commit()
        c.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage
                     WHERE service = 'claude-notes-question'
                       AND used_at >= CURRENT_DATE""")
        spent_today = float(c.fetchone()[0] or 0)
        conn.close()
    except Exception as e:
        print('[notes-ask] could not record cost: ' + str(e)[:140])

    return jsonify({'answer': answer, 'notes_used': len(rows), 'total_matching': total,
                    'truncated': total > len(rows),
                    'cost_usd': round(cost, 4),
                    'input_tokens': in_tok, 'output_tokens': out_tok,
                    'spent_today_usd': round(spent_today, 4)})


@app.route('/api/analytics/notes-content', methods=['GET'])
@require_manager
def notes_content_analytics():
    """What agents actually write after a call.

    The existing notes page scores quality; this shows the content itself —
    the phrases that come up again and again, notes copied word for word
    between calls, how long they are, and who writes what. Reads VoiceGuard's
    own call records, so it needs nothing from the CMS.
    """
    import re
    from collections import Counter

    date_from = request.args.get('date_from') or ''
    date_to = request.args.get('date_to') or ''
    agent = request.args.get('agent') or ''
    limit = min(50000, max(100, int(request.args.get('limit') or 20000)))

    where, params = ['1=1'], []
    if date_from:
        where.append('created_at >= %s'); params.append(date_from + ' 00:00:00')
    if date_to:
        where.append('created_at <= %s'); params.append(date_to + ' 23:59:59')
    if agent:
        where.append('agent_name = %s'); params.append(agent)

    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("""SELECT agent_name, call_notes, notes_score, created_at
                 FROM calls WHERE %s ORDER BY created_at DESC LIMIT %s"""
              % (' AND '.join(where), limit), params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    total = len(rows)
    with_notes = [r for r in rows if (r.get('call_notes') or '').strip()]
    missing = total - len(with_notes)

    lengths = [len((r['call_notes'] or '').strip()) for r in with_notes]
    words_per = [len(_note_words(r['call_notes'])) for r in with_notes]
    lengths_sorted = sorted(lengths)
    median = lengths_sorted[len(lengths_sorted)//2] if lengths_sorted else 0

    # how substantial are they? buckets people can act on
    buckets = {'empty': missing, 'under 10 chars': 0, '10-40': 0, '40-120': 0, 'over 120': 0}
    for L in lengths:
        if L < 10: buckets['under 10 chars'] += 1
        elif L < 40: buckets['10-40'] += 1
        elif L < 120: buckets['40-120'] += 1
        else: buckets['over 120'] += 1

    # phrases that keep coming up (two and three words, stopwords dropped)
    bi, tri, uni = Counter(), Counter(), Counter()
    for r in with_notes:
        ws = [w for w in _note_words(r['call_notes'])]
        keep = [w for w in ws if w not in NOTE_STOPWORDS]
        uni.update(keep)
        for i in range(len(ws) - 1):
            if ws[i] in NOTE_STOPWORDS and ws[i+1] in NOTE_STOPWORDS:
                continue
            bi[' '.join(ws[i:i+2])] += 1
        for i in range(len(ws) - 2):
            if all(w in NOTE_STOPWORDS for w in ws[i:i+3]):
                continue
            tri[' '.join(ws[i:i+3])] += 1

    # notes written word for word the same on different calls — a template,
    # a copy-paste habit, or an agent not really writing anything
    exact = Counter()
    for r in with_notes:
        t = re.sub(r'\s+', ' ', (r['call_notes'] or '').strip().lower())
        if len(t) >= 5:
            exact[t] += 1
    repeated = [{'text': t[:200], 'times': n} for t, n in exact.most_common(25) if n > 1]
    repeated_share = round(sum(n for _, n in exact.items() if n > 1) / max(1, len(with_notes)) * 100, 1)

    # per agent
    per = {}
    for r in rows:
        a = r.get('agent_name') or '(unknown)'
        d = per.setdefault(a, {'agent': a, 'calls': 0, 'with_notes': 0, 'chars': 0,
                               'words': 0, 'scores': [], 'texts': []})
        d['calls'] += 1
        t = (r.get('call_notes') or '').strip()
        if t:
            d['with_notes'] += 1
            d['chars'] += len(t)
            d['words'] += len(_note_words(t))
            d['texts'].append(re.sub(r'\s+', ' ', t.lower()))
        if r.get('notes_score') is not None:
            try: d['scores'].append(float(r['notes_score']))
            except Exception: pass
    per_agent = []
    for d in per.values():
        n = max(1, d['with_notes'])
        dup = len(d['texts']) - len(set(d['texts']))
        per_agent.append({
            'agent': d['agent'], 'calls': d['calls'],
            'missing_notes': d['calls'] - d['with_notes'],
            'missing_pct': round((d['calls'] - d['with_notes']) / max(1, d['calls']) * 100, 1),
            'avg_chars': round(d['chars'] / n),
            'avg_words': round(d['words'] / n, 1),
            'repeated_notes': dup,
            'repeated_pct': round(dup / n * 100, 1),
            'avg_score': round(sum(d['scores']) / len(d['scores']), 1) if d['scores'] else None,
        })
    per_agent.sort(key=lambda x: -x['calls'])

    # a few real examples, so the numbers stay grounded
    shortest = sorted([r for r in with_notes if len((r['call_notes'] or '').strip()) < 25],
                      key=lambda r: len(r['call_notes']))[:12]
    return jsonify({
        'total_calls': total,
        'with_notes': len(with_notes),
        'missing_notes': missing,
        'missing_pct': round(missing / max(1, total) * 100, 1),
        'avg_chars': round(sum(lengths) / max(1, len(lengths))),
        'median_chars': median,
        'avg_words': round(sum(words_per) / max(1, len(words_per)), 1),
        'buckets': buckets,
        'top_words': uni.most_common(40),
        'top_phrases': bi.most_common(30),
        'top_trigrams': tri.most_common(20),
        'repeated': repeated,
        'repeated_share_pct': repeated_share,
        'per_agent': per_agent,
        'examples_short': [{'agent': r.get('agent_name'), 'text': (r['call_notes'] or '')[:120]}
                           for r in shortest],
        'date_from': date_from, 'date_to': date_to, 'agent': agent,
    })


@app.route('/api/cms-db/analyze', methods=['GET'])
@require_manager
def cms_db_analyze():
    """Group and total any CMS table — orders by month, by agent, by status.
    Read-only, and every table/column name is validated before it reaches SQL."""
    import cms_db
    a = request.args
    try:
        return jsonify(cms_db.aggregate(
            table=a.get('table', ''),
            date_col=a.get('date_col') or None,
            date_from=(a.get('date_from') or None),
            date_to=(a.get('date_to') or None),
            group_col=a.get('group_col') or None,
            period=a.get('period') or None,
            value_col=a.get('value_col') or None,
            metric=a.get('metric', 'count'),
            database=a.get('database') or None,
            limit=int(a.get('limit') or 500)))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


PAYMENTS_CODE_DEFAULT = '2317'


def _payments_code():
    """The code that guards the payments page. Kept in settings so it can be
    changed without a deploy; never sent to the browser."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'payments_code'")
        r = c.fetchone(); conn.close()
        if r and r[0]:
            return str(r[0]).strip()
    except Exception:
        pass
    return PAYMENTS_CODE_DEFAULT


def _payments_ticket(valid_minutes=45):
    """A short-lived pass issued after the code is entered correctly. It is
    signed with the app's secret, so the browser can hold it but cannot forge
    one, and it expires on its own."""
    import hmac, hashlib, base64, time
    exp = int(time.time()) + valid_minutes * 60
    secret = (os.getenv('SECRET_KEY') or app.secret_key or 'voiceguard').encode()
    sig = hmac.new(secret, str(exp).encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(('%d.%s' % (exp, sig)).encode()).decode()


def _payments_ticket_ok(ticket):
    import hmac, hashlib, base64, time
    try:
        raw = base64.urlsafe_b64decode(str(ticket).encode()).decode()
        exp_s, sig = raw.split('.', 1)
        if int(exp_s) < time.time():
            return False
        secret = (os.getenv('SECRET_KEY') or app.secret_key or 'voiceguard').encode()
        want = hmac.new(secret, exp_s.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, want)
    except Exception:
        return False


def require_payments_code(f):
    """Manager login AND the page code. The code is a second lock on top of the
    normal login, so someone already signed in still cannot open this page."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _payments_ticket_ok(request.headers.get('X-Payments-Ticket', '')):
            return jsonify({'error': 'locked', 'need_code': True}), 403
        return f(*args, **kwargs)
    return decorated


def _setting(key, default=''):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT value FROM app_settings WHERE key = %s', (key,))
        r = c.fetchone(); conn.close()
        return (r[0] if r and r[0] else '') or default
    except Exception:
        return default


# ---------------------------------------------------------------- portal ----
# A second way in, for the people who already have CMS accounts. It serves the
# same pages but only the ones built on the CMS, and it signs people in with
# their existing user name and password rather than a second set of logins.
PORTAL_PAGES = ['live', 'customers', 'agent-calls']          # everyone
PORTAL_MANAGER_PAGES = ['payments', 'cms-settings']          # managers only


def _portal_ticket(user, minutes=600):
    import hmac, hashlib, base64, time, json as _json
    exp = int(time.time()) + minutes * 60
    body = _json.dumps({'u': user.get('user_id'), 'e': user.get('employee_id'),
                        'n': user.get('name'), 'm': bool(user.get('is_manager')),
                        'x': exp}, separators=(',', ':'))
    secret = (os.getenv('SECRET_KEY') or app.secret_key or 'voiceguard').encode()
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode((body + '.' + sig).encode()).decode()


def _portal_user(ticket):
    """Returns the signed-in person, or None. The signature is checked before
    anything in the ticket is believed."""
    import hmac, hashlib, base64, time, json as _json
    try:
        raw = base64.urlsafe_b64decode(str(ticket).encode()).decode()
        body, sig = raw.rsplit('.', 1)
        secret = (os.getenv('SECRET_KEY') or app.secret_key or 'voiceguard').encode()
        want = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            return None
        data = _json.loads(body)
        if int(data.get('x', 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def portal_or_manager(f):
    """Either a VoiceGuard manager login or a valid CMS portal sign-in."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if _portal_user(request.headers.get('X-Portal-Ticket', '')):
            return f(*args, **kwargs)
        user = get_token_user(get_request_token())
        if user and user.get('role') in ('admin', 'manager'):
            return f(*args, **kwargs)
        if session.get('logged_in'):
            return f(*args, **kwargs)
        return jsonify({'error': 'Sign in first'}), 401
    return decorated


def portal_manager_only(f):
    """Payments and settings: managers only, from either door."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        p = _portal_user(request.headers.get('X-Portal-Ticket', ''))
        if p and p.get('m'):
            return f(*args, **kwargs)
        if p:
            return jsonify({'error': 'Managers only'}), 403
        user = get_token_user(get_request_token())
        if (user and user.get('role') in ('admin', 'manager')) or session.get('logged_in'):
            return f(*args, **kwargs)
        return jsonify({'error': 'Managers only'}), 403
    return decorated


@app.route('/portal')
@app.route('/portal/')
def portal_page():
    """The CMS side of the system, on its own address."""
    try:
        with open('qa-dashboard.html', 'r', encoding='utf-8') as fh:
            html = fh.read()
    except Exception:
        return 'Portal page missing', 500
    html = html.replace('<body>', '<body data-portal="1">', 1)
    resp = make_response(html)
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-Build'] = SERVER_BUILD
    return resp


@app.route('/api/portal/diagnose', methods=['POST'])
@require_manager
def portal_diagnose():
    """Why a CMS sign-in is being refused.

    Reports what the account looks like without revealing anything secret: is
    the user name found at all, what the stored hash format is, whether it is
    linked to an Employee, and what roles it has. Only reachable from the QA
    dashboard by a manager.
    """
    import cms_db, base64
    d = request.json or {}
    u = (d.get('username') or '').strip()
    pw = d.get('password') or ''
    if not u:
        return jsonify({'error': 'username required'}), 400
    out = {'looked_for': u}
    try:
        conn = cms_db._connect(); cu = conn.cursor()
        cu.execute("""SELECT TOP 5 Id, UserName, Email, PasswordHash, LockoutEnabled,
                             LockoutEndDateUtc, EmailConfirmed
                      FROM AspNetUsers
                      WHERE UserName = %s OR Email = %s
                         OR UserName LIKE %s OR Email LIKE %s""",
                   (u, u, '%' + u + '%', '%' + u + '%'))
        matches = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            h = r[3] or ''
            fmt, note = 'unknown', ''
            try:
                blob = base64.b64decode(h)
                if blob and blob[0] == 0x00:
                    fmt = 'ASP.NET Identity v2'
                    note = 'supported' if len(blob) == 49 else 'unexpected length %d' % len(blob)
                elif blob and blob[0] == 0x01:
                    fmt = 'ASP.NET Identity v3'; note = 'supported'
                elif not h:
                    fmt = 'no password set'; note = 'this account cannot sign in'
                else:
                    fmt = 'something else (first byte %d)' % blob[0]
                    note = 'not a format we can check'
            except Exception:
                fmt = 'not base64'
                note = 'length %d — may be a different hashing scheme' % len(h)
            matches.append({'user_name': r[1], 'email': r[2],
                            'hash_format': fmt, 'hash_note': note,
                            'hash_length': len(h),
                            'lockout_enabled': bool(r[4]),
                            'locked_until': cms_db._plain(r[5]),
                            'exact_match': (r[1] == u or r[2] == u)})
        out['accounts_found'] = matches
        cu.execute('SELECT COUNT(*) FROM AspNetUsers')
        out['total_accounts'] = int((cu.fetchone() or [0])[0] or 0)
        conn.close()
    except Exception as e:
        out['error'] = str(e)[:200]
        return jsonify(out), 400

    if pw:
        try:
            user = cms_db.portal_login(u, pw)
            out['sign_in'] = 'works — ' + user['name'] + (' (manager)' if user['is_manager'] else '')
        except Exception as e:
            out['sign_in'] = 'refused: ' + str(e)[:160]

    if not matches:
        out['meaning'] = ('No account in AspNetUsers matches that, even loosely. '
                          'There are %d accounts in total — the sign-in name may differ '
                          'from what people use day to day.' % out.get('total_accounts', 0))
    elif not any(m['exact_match'] for m in matches):
        out['meaning'] = ('Nothing matches exactly, but similar names exist — try one of '
                          'the user names listed above.')
    else:
        out['meaning'] = 'The account exists. See the hash format and the sign-in result above.'
    return jsonify(out)


@app.route('/api/portal/login', methods=['POST'])
def portal_login_route():
    """Sign in with a CMS user name and password."""
    import cms_db
    d = request.json or {}
    import time as _t
    _t.sleep(0.35)                       # slows down anyone trying passwords in bulk
    try:
        user = cms_db.portal_login(d.get('username'), d.get('password'))
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:160]}), 403
    return jsonify({'ok': True, 'ticket': _portal_ticket(user),
                    'name': user['name'], 'is_manager': user['is_manager'],
                    'extension': user.get('extension'),
                    'pages': PORTAL_PAGES + (PORTAL_MANAGER_PAGES if user['is_manager'] else [])})


@app.route('/api/portal/me', methods=['GET'])
def portal_me():
    p = _portal_user(request.headers.get('X-Portal-Ticket', ''))
    if not p:
        return jsonify({'signed_in': False}), 401
    return jsonify({'signed_in': True, 'name': p.get('n'), 'is_manager': bool(p.get('m')),
                    'pages': PORTAL_PAGES + (PORTAL_MANAGER_PAGES if p.get('m') else [])})


@app.route('/api/recording-base/find', methods=['GET', 'POST'])
@require_manager
def recording_base_find():
    """Finds the recording address and proves it by fetching a real recording.

    Each candidate is tried against an actual path from the call log. The first
    that answers is saved, so recording links start working without anyone
    having to be asked.
    """
    import cms_db
    try:
        info = cms_db.find_recording_base()
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400

    tried, winner = [], None
    sample = (info.get('sample_paths') or [None])[0]
    if sample:
        for base in info['candidates']:
            url = base.rstrip('/') + '/' + sample.lstrip('/')
            entry = {'base': base, 'url': url}
            try:
                req = urllib.request.Request(url, method='HEAD',
                                             headers={'User-Agent': 'VoiceGuard/1.0'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    entry['status'] = r.status
                    entry['type'] = r.headers.get('Content-Type', '')
                    entry['works'] = r.status < 400 and 'html' not in entry['type'].lower()
            except Exception as e:
                entry['status'] = None
                entry['works'] = False
                entry['error'] = str(e)[:110]
            tried.append(entry)
            if entry.get('works'):
                winner = base
                break

    saved = False
    if winner and request.method == 'POST':
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("""INSERT INTO app_settings (key, value) VALUES ('recording_base_url', %s)
                         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (winner,))
            conn.commit(); conn.close()
            saved = True
        except Exception as e:
            info['notes'].append('could not save: ' + str(e)[:120])

    return jsonify({**info, 'tried': tried, 'winner': winner, 'saved': saved,
                    'meaning': (('Recordings are served from %s%s' % (winner, ' — saved.' if saved else '.'))
                                if winner else
                                'None of the addresses tried returned a recording. '
                                'Ask Igor what address recordings are served from, or whether '
                                'they need a login.')})


@app.route('/api/connections', methods=['GET'])
@require_manager
def connections_check():
    """Tests every link between VoiceGuard, the phone system and the CMS.

    Each check answers the same question in plain terms: is this wire connected,
    and if not, what exactly is missing. Nothing here changes anything.
    """
    import socket, time as _t
    checks = []

    def add(name, purpose, ok, detail, fix=''):
        checks.append({'name': name, 'purpose': purpose, 'ok': bool(ok),
                       'detail': detail, 'fix': fix})

    # 1 — calls arriving from the phone system
    try:
        conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("""SELECT COUNT(*) AS n, MAX(created_at) AS last
                     FROM calls WHERE created_at >= NOW() - INTERVAL '24 hours'""")
        r = dict(c.fetchone())
        c.execute("SELECT COUNT(*) AS n FROM calls")
        total = dict(c.fetchone())['n']
        conn.close()
        last = r['last']
        mins = int((datetime.now() - last).total_seconds() / 60) if last else None
        add('Calls arriving', 'The phone system posts each finished call to /api/analyze',
            bool(last and mins is not None and mins < 240),
            (('%d calls in the last 24 hours · most recent %d minutes ago' % (r['n'], mins))
             if last else 'No calls have ever arrived') + ' · %d in total' % total,
            'Ask Igor to confirm the phone system is still posting to /api/analyze on this server.')
    except Exception as e:
        add('Calls arriving', 'The phone system posts each finished call', False, str(e)[:160], '')

    # 2 — agent availability API
    try:
        import phone_status as PS
        if not PS.configured():
            add('Agent availability', 'Reads who was reachable, from the phone system API',
                False, 'No API key is set (PHONE_API_KEY).',
                'Add PHONE_API_KEY in the App Service settings with the token from Igor.')
        else:
            t0 = _t.time()
            today = datetime.now().strftime('%Y-%m-%d')
            rows_ = PS.fetch(today + ' 00:00:00', today + ' 23:59:59', timeout=20)
            ms = int((_t.time() - t0) * 1000)
            conn = get_db(); c = conn.cursor()
            c.execute('SELECT COUNT(*), MAX(snapshot_at) FROM phone_status_snapshots')
            stored, newest = c.fetchone(); conn.close()
            add('Agent availability', 'Reads who was reachable, from the phone system API',
                True, '%d rows returned in %dms · %s stored here%s'
                % (len(rows_), ms, format(int(stored or 0), ','),
                   (' · newest ' + newest.strftime('%d %b %H:%M')) if newest else ' · nothing stored yet'),
                '' if stored else 'Run a collection from the Time Report page to start storing it.')
    except Exception as e:
        add('Agent availability', 'Reads who was reachable, from the phone system API',
            False, str(e)[:200], 'Check PHONE_API_KEY and that the API is reachable.')

    # 3 — the CMS database
    try:
        import cms_db
        t = cms_db.test()
        add('CMS database', 'Customers, calls, payments and settings',
            bool(t.get('ok')),
            (('%s · %s' % (t.get('detected_type') or t.get('type'), t.get('version', '')[:40]))
             if t.get('ok') else t.get('meaning', '')),
            '' if t.get('ok') else 'Check CMS_DB_HOST / NAME / USER / PASSWORD.')
    except Exception as e:
        add('CMS database', 'Customers, calls, payments and settings', False, str(e)[:180],
            'Set the CMS_DB_* settings on the App Service.')

    # 4 — recordings
    base = _setting('recording_base_url', os.getenv('RECORDING_BASE_URL', ''))
    if not base:
        add('Call recordings', 'Turns a stored path into a link that opens', False,
            'No address is set, so recording links cannot be built.',
            "Add a setting called recording_base_url with the address recordings are served from "
            "(ask Igor), e.g. https://recordings.example.com")
    else:
        try:
            req = urllib.request.Request(base, method='HEAD',
                                         headers={'User-Agent': 'VoiceGuard/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                add('Call recordings', 'Turns a stored path into a link that opens',
                    True, '%s answers (HTTP %d)' % (base, r.status), '')
        except Exception as e:
            add('Call recordings', 'Turns a stored path into a link that opens',
                False, '%s did not answer: %s' % (base, str(e)[:120]),
                'Check the address, or whether it needs a login.')

    # 5 — the recording relay
    if not RELAY_URL:
        add('Recording relay', 'Downloads audio when this server cannot reach it directly',
            True, 'Not in use — audio is fetched directly.', '')
    else:
        try:
            req = urllib.request.Request(RELAY_URL.rstrip('/') + '/health',
                                         headers={'User-Agent': 'VoiceGuard/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                add('Recording relay', 'Downloads audio when this server cannot reach it directly',
                    r.status < 400, '%s answered HTTP %d' % (RELAY_URL, r.status), '')
        except Exception as e:
            add('Recording relay', 'Downloads audio when this server cannot reach it directly',
                False, str(e)[:160], 'The relay app may be stopped — check it in Azure.')

    # 6 — the scoring engines
    add('Claude', 'Scores calls and answers questions about notes',
        bool(ANTHROPIC_API_KEY and 'your_' not in ANTHROPIC_API_KEY),
        'Key is set' if ANTHROPIC_API_KEY else 'No key set',
        '' if ANTHROPIC_API_KEY else 'Add ANTHROPIC_API_KEY in the App Service settings.')
    gem = os.getenv('GEMINI_API_KEY', '')
    add('Gemini', 'Transcribes the audio',
        bool(gem and 'your_' not in gem), 'Key is set' if gem else 'No key set',
        '' if gem else 'Add GEMINI_API_KEY in the App Service settings.')

    # 7 — this server's own address, for anyone allowlisting us
    out_ip = None
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=6) as r:
            out_ip = r.read().decode().strip()
    except Exception:
        pass

    ok_count = sum(1 for c_ in checks if c_['ok'])
    return jsonify({'checks': checks, 'ok': ok_count, 'total': len(checks),
                    'outbound_ip': out_ip,
                    'analyze_url': request.url_root.rstrip('/') + '/api/analyze',
                    'as_of': datetime.now().isoformat()})


@app.route('/api/cms-db/write-readiness', methods=['GET'])
@require_manager
def cms_write_readiness():
    """Can this login change data, and can it make a practice copy?"""
    import cms_db
    try:
        return jsonify(cms_db.write_readiness())
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-settings', methods=['GET'])
@portal_manager_only
def cms_settings_route():
    """Every configuration table in the CMS, read only. Secret keys are masked
    before they leave the server."""
    import cms_db
    try:
        return jsonify(cms_db.settings_all())
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/customers/recent', methods=['GET'])
@portal_or_manager
def customers_recent():
    """What to show before anyone searches: just worked on, and just created."""
    import cms_db
    try:
        return jsonify(cms_db.recent_accounts())
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/customers/search', methods=['GET'])
@portal_or_manager
def customers_search():
    """Find an account by name, phone or email."""
    import cms_db
    try:
        return jsonify({'items': cms_db.customer_search(request.args.get('q', ''))})
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/customers/<int:account_id>', methods=['GET'])
@portal_or_manager
def customer_profile_route(account_id):
    """Everything held about one account."""
    import cms_db
    try:
        return jsonify(cms_db.customer_profile(account_id))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/live', methods=['GET'])
@portal_or_manager
def live_route():
    """The whole floor in one read — agents, calls in progress, and the feed."""
    import cms_db
    try:
        out = cms_db.live_snapshot()
        out['as_of'] = datetime.now().isoformat()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/agents/live', methods=['GET'])
@portal_or_manager
def agents_live_route():
    """Who is on shift and on the phone right now."""
    import cms_db
    try:
        return jsonify({'agents': cms_db.agents_live(),
                        'as_of': datetime.now().isoformat()})
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/agents/<int:employee_id>/detail', methods=['GET'])
@portal_or_manager
def agent_detail_route(employee_id):
    """One agent's calls, notes, clock events and sales for a date range."""
    import cms_db
    a = request.args
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        return jsonify(cms_db.agent_detail(
            employee_id, (a.get('date_from') or today)[:10], (a.get('date_to') or today)[:10]))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/recording-link', methods=['GET'])
@portal_or_manager
def recording_link():
    """Turns a stored recording path into a link that opens.

    The CMS keeps a relative path like recordings/2026/08/20/abc.wav; the base
    address it hangs off is a setting so it can be corrected without a deploy.
    """
    path = (request.args.get('path') or '').strip()
    if not path:
        return jsonify({'error': 'path required'}), 400
    base = ''
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'recording_base_url'")
        r = c.fetchone(); conn.close()
        base = (r[0] if r and r[0] else '') or os.getenv('RECORDING_BASE_URL', '')
    except Exception:
        base = os.getenv('RECORDING_BASE_URL', '')
    if path.lower().startswith(('http://', 'https://')):
        return jsonify({'url': path})          # already a full address
    if not base:
        return jsonify({'error': 'No recording address is set yet (recording_base_url).',
                        'path': path}), 400
    return jsonify({'url': base.rstrip('/') + '/' + path.lstrip('/')})


@app.route('/api/payments/unlock', methods=['POST'])
@portal_manager_only
def payments_unlock():
    """Check the code and hand back a pass that expires."""
    code = str((request.json or {}).get('code') or '').strip()
    import time
    time.sleep(0.4)                      # slows down anyone trying codes in bulk
    if code != _payments_code():
        return jsonify({'ok': False, 'error': 'That code is not right.'}), 403
    return jsonify({'ok': True, 'ticket': _payments_ticket(), 'minutes': 45})


@app.route('/api/payments/data', methods=['GET'])
@portal_manager_only
@require_payments_code
def payments_data():
    """Everything the payments page shows, for the chosen dates and filters."""
    import cms_db
    a = request.args
    try:
        return jsonify(cms_db.payments(
            (a.get('date_from') or '')[:10], (a.get('date_to') or '')[:10],
            employee_id=a.get('employee_id') or None,
            account_id=a.get('account_id') or None,
            account_search=(a.get('account_search') or '').strip() or None))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/payments/people', methods=['GET'])
@portal_manager_only
@require_payments_code
def payments_people():
    """Names for the agent and account pickers."""
    import cms_db
    try:
        return jsonify({'items': cms_db.payment_people(
            request.args.get('kind', 'agents'), request.args.get('q', ''))})
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/schema', methods=['GET'])
@require_manager
def cms_db_schema():
    """The whole shape of a database — tables, columns, keys and how they link.
    Returned as JSON, or as plain text with ?format=text for downloading."""
    import cms_db
    try:
        rep = cms_db.schema_report(
            request.args.get('database') or None,
            include_samples=(request.args.get('samples') == '1'))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400
    if request.args.get('format') == 'text':
        from flask import Response
        return Response(cms_db.schema_markdown(rep), mimetype='text/plain')
    return jsonify(rep)


@app.route('/api/cms-db/find-topic', methods=['GET'])
@require_manager
def cms_db_find_topic():
    """Which tables hold a kind of data — payments, orders, customers — judged
    by column names rather than a specific value."""
    import cms_db
    try:
        return jsonify(cms_db.find_topic(request.args.get('topic', ''),
                                         request.args.get('database') or None))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/find-value', methods=['GET'])
@require_manager
def cms_db_find_value():
    """Search the database for a value you can see on a CMS screen — an
    extension, an email, a name — and report which table and column holds it.
    Much more reliable than guessing table names."""
    import cms_db
    try:
        return jsonify(cms_db.find_value(
            request.args.get('value', ''),
            request.args.get('database') or None,
            all_databases=(request.args.get('all') == '1')))
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/sample', methods=['GET'])
@require_manager
def cms_db_sample():
    """Columns and rows of one table, from any database on the server."""
    import cms_db
    table = request.args.get('table', '')
    db = request.args.get('database') or None
    try:
        out = cms_db.sample(table, request.args.get('limit', 15), db)
        try:
            out.update(cms_db.columns(table, db))
        except Exception:
            pass
        out['database'] = db or cms_db.NAME
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400


@app.route('/api/cms-db/mapping', methods=['GET', 'POST'])
@require_manager
def cms_db_mapping():
    """Which table and columns hold the clock events. Saved once, then reused
    by every import — every CMS names these differently."""
    conn = get_db(); c = conn.cursor()
    if request.method == 'POST':
        m = request.json or {}
        keep = {k: (m.get(k) or '') for k in
                ('table', 'employee', 'time', 'status', 'break_minutes', 'break_reason')}
        c.execute("""INSERT INTO app_settings (key, value) VALUES ('cms_db_mapping', %s)
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                  (json.dumps(keep),))
        conn.commit(); conn.close()
        return jsonify({'saved': True, 'mapping': keep})
    c.execute("SELECT value FROM app_settings WHERE key = 'cms_db_mapping'")
    r = c.fetchone(); conn.close()
    try:
        return jsonify({'mapping': json.loads(r[0]) if r and r[0] else {}})
    except Exception:
        return jsonify({'mapping': {}})


@app.route('/api/cms-db/preview', methods=['GET'])
@require_manager
def cms_db_preview():
    """Read clock events for a date range and just show them — nothing is saved.

    This is the "does the data look right?" step. It returns the same fields the
    uploaded spreadsheet has, so it can be compared against a familiar export
    before anything is trusted or stored.
    """
    import cms_db
    date_from = (request.args.get('date_from') or '')[:10]
    date_to = (request.args.get('date_to') or '')[:10]
    if not date_from or not date_to:
        return jsonify({'error': 'Choose both dates'}), 400

    conn = get_db(); c = conn.cursor()
    c.execute("SELECT value FROM app_settings WHERE key = 'cms_db_mapping'")
    r = c.fetchone(); conn.close()
    try:
        mapping = json.loads(r[0]) if r and r[0] else {}
    except Exception:
        mapping = {}
    if not mapping.get('table'):
        return jsonify({'error': 'Set the table and columns first (step 2 and 3 above)'}), 400

    try:
        events = cms_db.fetch_events(mapping, date_from, date_to)
    except Exception as e:
        return jsonify({'error': str(e)[:240]}), 400

    by_agent = {}
    by_status = {}
    for e in events:
        by_agent[e['employee_name']] = by_agent.get(e['employee_name'], 0) + 1
        by_status[e['status']] = by_status.get(e['status'], 0) + 1
    days = sorted({(e['event_time'] or '')[:10] for e in events if e['event_time']})
    return jsonify({
        'events': events[:5000],
        'total': len(events),
        'truncated': len(events) > 5000,
        'agents': sorted(by_agent.items(), key=lambda x: -x[1]),
        'statuses': sorted(by_status.items(), key=lambda x: -x[1]),
        'days': days,
        'date_from': date_from, 'date_to': date_to,
        'stored': False,
    })


@app.route('/api/cms-db/import', methods=['POST', 'GET'])
@require_manager
def cms_db_import():
    """Pull clock events for a date range straight from the CMS and store them
    exactly as the spreadsheet upload does — so shifts, overtime and pay are
    calculated the same way, with no manual export step.

    Safe to re-run: identical events are ignored rather than duplicated.
    """
    import cms_db
    d = (request.json or {}) if request.method == 'POST' else {}
    date_from = (d.get('date_from') or request.args.get('date_from') or '')[:10]
    date_to = (d.get('date_to') or request.args.get('date_to') or '')[:10]
    if not date_from or not date_to:
        return jsonify({'error': 'date_from and date_to are required'}), 400

    conn = get_db(); c = conn.cursor()
    c.execute("SELECT value FROM app_settings WHERE key = 'cms_db_mapping'")
    r = c.fetchone()
    mapping = {}
    try:
        mapping = json.loads(r[0]) if r and r[0] else {}
    except Exception:
        pass
    if not mapping.get('table'):
        conn.close()
        return jsonify({'error': 'No column mapping saved yet — set it on the Time Report page first'}), 400

    try:
        events = cms_db.fetch_events(mapping, date_from, date_to)
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)[:240]}), 400

    stored = 0
    for e in events:
        try:
            c.execute("""INSERT INTO clocker_events_cms
                            (employee_name, event_time, status, break_minutes, break_reason)
                         VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (employee_name, event_time, status) DO NOTHING""",
                      (e['employee_name'], e['event_time'], e['status'],
                       e['break_minutes'], e['break_reason']))
            stored += c.rowcount
        except Exception as ex:
            print('[cms_db] skipped an event: ' + str(ex)[:120])
    conn.commit(); conn.close()
    return jsonify({'fetched': len(events), 'stored': stored,
                    'already_had': len(events) - stored,
                    'date_from': date_from, 'date_to': date_to,
                    'agents': len({e['employee_name'] for e in events})})


@app.route('/api/phone-status/collect', methods=['POST', 'GET'])
@require_manager
def phone_status_collect():
    """Pull availability snapshots from the phone system and store them.

    Called on a schedule for 'today', and used with an explicit range to backfill
    history — the API only serves 7 days, so anything older exists only if we
    collected it. Re-running a range is safe; duplicates are ignored.
    """
    import phone_status as PS
    if not PS.configured():
        return jsonify({'error': 'PHONE_API_KEY is not set on the server'}), 400
    d = (request.json or {}) if request.method == 'POST' else {}
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = (d.get('date_from') or request.args.get('date_from') or today)[:10]
    date_to = (d.get('date_to') or request.args.get('date_to') or today)[:10]
    conn = get_db()
    try:
        result = PS.collect(conn, date_from, date_to)
        result['interval_seconds'] = PS.detect_interval(conn)
    finally:
        conn.close()
    result['date_from'], result['date_to'] = date_from, date_to
    return jsonify(result)


@app.route('/api/phone-status/status', methods=['GET'])
@require_manager
def phone_status_status():
    """What availability data we hold, so it's obvious whether a gap in the
    report is an agent being offline or us simply not having collected."""
    import phone_status as PS
    conn = get_db(); c = conn.cursor(cursor_factory=RealDictCursor)
    out = {'configured': PS.configured(), 'api_url': PS.API_URL}
    try:
        c.execute("""SELECT COUNT(*) AS rows, MIN(snapshot_at) AS oldest,
                            MAX(snapshot_at) AS newest,
                            COUNT(DISTINCT employee_id) AS agents
                     FROM phone_status_snapshots""")
        r = dict(c.fetchone())
        out.update({
            'rows': r['rows'],
            'agents': r['agents'],
            'oldest': r['oldest'].isoformat() if r['oldest'] else None,
            'newest': r['newest'].isoformat() if r['newest'] else None,
        })
        out['interval_seconds'] = PS.detect_interval(conn)
    except Exception as e:
        out['error'] = str(e)[:200]
    finally:
        conn.close()
    return jsonify(out)


@app.route('/api/time-report', methods=['GET'])
@require_manager
def time_report():
    try:
        return _time_report_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[time_report] FAILED:\n{tb}")
        return jsonify({'error': f'Time report failed: {str(e)[:300]}',
                        'report': [], 'count': 0,
                        'totals': {}, 'per_agent': []}), 500

def _availability_for_shift(conn, ext, emp_id, start, end, interval):
    """Availability inside one shift, matched on extension or employee id."""
    if start is None or end is None or (not ext and emp_id is None):
        return None
    c = conn.cursor()
    c.execute("""SELECT snapshot_at, phone_status, cms_status FROM phone_status_snapshots
                 WHERE snapshot_at >= %s AND snapshot_at < %s
                   AND (agent_ext = %s OR (employee_id IS NOT NULL AND employee_id = %s))
                 ORDER BY snapshot_at""",
              (start, end, ext or '', emp_id))
    raw = c.fetchall()
    rows = [(p, m) for _, p, m in raw]
    if not raw:
        return None
    if not rows:
        return None
    step = (interval or 60) / 60.0
    n_av = sum(1 for p, m in rows if p and m == 1)
    n_br = sum(1 for p, m in rows if p and m == 2)
    n_dnd = sum(1 for p, m in rows if p and m == 3)
    n_off = sum(1 for p, m in rows if not p)
    total = len(rows)
    shift_min = max(0.0, (end - start).total_seconds() / 60.0)

    # When did they actually become reachable, and when did they stop being so?
    # Clocking into the CMS is not the same as being ready to take a call.
    ready_at = next((t for t, p, m in raw if p and m == 1), None)
    last_ready = next((t for t, p, m in reversed(raw) if p and m == 1), None)
    late_min = round((ready_at - start).total_seconds() / 60.0, 1) if ready_at else None
    early_off_min = round((end - last_ready).total_seconds() / 60.0, 1) if last_ready else None

    # Every stretch where they couldn't take a call, anywhere in the shift.
    #
    # CMS break is deliberately NOT counted here: break time is already taken
    # out of net hours, so deducting it again would charge the agent twice for
    # the same minutes.
    def _reason(p, m):
        if not p:
            return 'phone offline'
        if m == 3:
            return 'DND'
        if m == 0:
            return 'not active in CMS'
        return None                      # 1 = available, 2 = break (already excluded)

    gaps = []
    run_start, run_reason, run_n = None, None, 0
    for t, p, m in raw:
        why = _reason(p, m)
        if why:
            if run_start is None:
                run_start, run_reason, run_n = t, why, 1
            else:
                run_n += 1
                if why != run_reason:
                    run_reason = 'mixed'
        elif run_start is not None:
            gaps.append({'from': run_start.isoformat(), 'to': t.isoformat(),
                         'minutes': round(run_n * ((interval or 60) / 60.0), 1),
                         'reason': run_reason})
            run_start, run_reason, run_n = None, None, 0
    if run_start is not None:
        gaps.append({'from': run_start.isoformat(), 'to': end.isoformat(),
                     'minutes': round(run_n * ((interval or 60) / 60.0), 1),
                     'reason': run_reason})

    return {
        'gaps': gaps,
        'unavailable_minutes': round(sum(g['minutes'] for g in gaps), 1),
        'ready_at': ready_at.isoformat() if ready_at else None,
        'last_ready_at': last_ready.isoformat() if last_ready else None,
        'late_to_phone_minutes': max(0.0, late_min) if late_min is not None else None,
        'early_off_phone_minutes': max(0.0, early_off_min) if early_off_min is not None else None,
        'available_minutes': round(n_av * step, 1),
        'available_hours': round(n_av * step / 60.0, 2),
        'break_minutes': round(n_br * step, 1),
        'dnd_minutes': round(n_dnd * step, 1),
        'offline_minutes': round(n_off * step, 1),
        'available_pct': round(n_av / total * 100, 1),
        'coverage_pct': round(min(100.0, total * step / shift_min * 100), 1) if shift_min else None,
        'samples': total,
    }


def _phone_grace_minutes():
    """Minutes of slack before a late phone start is deducted — a phone takes a
    moment to register, and nobody should lose pay for that."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'phone_grace_minutes'")
        r = c.fetchone(); conn.close()
        if r and r[0] is not None:
            return max(0.0, min(60.0, float(r[0])))
    except Exception:
        pass
    return 5.0


def _phone_min_gap_minutes():
    """Gaps shorter than this are ignored as snapshot noise rather than treated
    as real time off the phone."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'phone_min_gap_minutes'")
        r = c.fetchone(); conn.close()
        if r and r[0] is not None:
            return max(0.0, min(30.0, float(r[0])))
    except Exception:
        pass
    return 2.0


def _phone_identity_map():
    """VoiceGuard knows agents by NAME; the phone system reports an extension and
    a numeric EmployeeId. This builds name -> (extension, employee_id).

    The link comes from the agents table, which already carries each agent's
    extension from the call webhook. Anyone without an extension there simply has
    no availability figure — better a blank than a wrong number attached to the
    wrong person.
    """
    ext_by_name, id_by_ext = {}, {}
    conn = get_db()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        try:
            c.execute("SELECT name, extension FROM agents WHERE extension IS NOT NULL AND extension <> ''")
            for r in c.fetchall():
                ext = str(r['extension']).strip()
                if ext and ext != '—':
                    ext_by_name[str(r['name']).strip().lower()] = ext
        except Exception as e:
            print('[time_report] agents table unavailable for phone mapping: ' + str(e)[:120])
        try:
            c.execute("""SELECT DISTINCT agent_ext, employee_id FROM phone_status_snapshots
                         WHERE employee_id IS NOT NULL""")
            for r in c.fetchall():
                id_by_ext[str(r['agent_ext']).strip()] = r['employee_id']
        except Exception:
            pass
    finally:
        conn.close()
    return ext_by_name, id_by_ext


def _time_report_impl():
    """
    Builds the attendance report: reconstructs actual shifts from clocker_events and
    compares each against the matching agent_schedules row.
    Query params: date_from, date_to (YYYY-MM-DD), employee (optional), grace_minutes.
    """
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    employee = request.args.get('employee', '')
    grace = int(request.args.get('grace_minutes', 5))

    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)

    # Pull events with a 1-day pad so shifts crossing midnight are complete
    q = 'SELECT employee_name, event_time, status, break_minutes FROM clocker_events'
    params, where = [], []
    if date_from: where.append("event_time >= %s::date - INTERVAL '1 day'"); params.append(date_from)
    if date_to: where.append("event_time <= %s::date + INTERVAL '2 days'"); params.append(date_to)
    if employee: where.append('employee_name = %s'); params.append(employee)
    if where: q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY employee_name, event_time'
    c.execute(q, params)
    all_events = [dict(r) for r in c.fetchall()]

    # Pull schedules for the window — specific dates plus expanded recurring patterns
    schedules = _resolve_schedules(date_from, date_to, employee)

    # Pay rates and any manager-declared special overtime periods.
    # Wrapped so a missing/not-yet-migrated table degrades to "no pay data"
    # instead of failing the whole report.
    rates, ot_periods = {}, []
    try:
        c.execute('SELECT employee_name, hourly_rate, effective_from FROM agent_rates ORDER BY employee_name, effective_from')
        for r in c.fetchall():
            rates.setdefault(r['employee_name'], []).append(
                {'hourly_rate': float(r['hourly_rate']), 'effective_from': r['effective_from']})
    except Exception as e:
        print(f"[time_report] agent_rates unavailable: {str(e)[:120]}")
        try: conn.rollback()
        except Exception: pass
    try:
        c.execute('SELECT * FROM ot_multiplier_periods ORDER BY starts_at')
        ot_periods = [dict(r) for r in c.fetchall()]
    except Exception as e:
        print(f"[time_report] ot_multiplier_periods unavailable: {str(e)[:120]}")
        try: conn.rollback()
        except Exception: pass

    adjustments = {}
    try:
        aq = 'SELECT * FROM shift_adjustments'
        aparams, awhere = [], []
        if date_from: awhere.append('shift_date >= %s'); aparams.append(date_from)
        if date_to: awhere.append('shift_date <= %s'); aparams.append(date_to)
        if employee: awhere.append('employee_name = %s'); aparams.append(employee)
        if awhere: aq += ' WHERE ' + ' AND '.join(awhere)
        c.execute(aq, aparams)
        for r in c.fetchall():
            adjustments[(r['employee_name'], str(r['shift_date']), r['block_no'] or 1)] = dict(r)
    except Exception as e:
        print(f"[time_report] shift_adjustments unavailable: {str(e)[:120]}")
        try: conn.rollback()
        except Exception: pass
    conn.close()

    # Group events per employee and reconstruct shifts
    by_emp = {}
    for e in all_events:
        by_emp.setdefault(e['employee_name'], []).append(e)
    shifts_by_emp = {name: _build_shifts_from_events(evs) for name, evs in by_emp.items()}

    rows = []
    matched_shift_keys = set()

    # Pre-assign each worked segment to exactly ONE scheduled block — the block whose
    # start time it's closest to. This keeps split shifts (e.g. 7am-12pm and 7pm-12am)
    # from stealing each other's segments, while still letting several segments of the
    # same block merge together (mid-shift logout and re-login).
    MAX_ASSIGN_HOURS = 6
    assignment = {}  # id(segment) -> schedule key
    for name, segs in shifts_by_emp.items():
        emp_scheds = [s for s in schedules if s['employee_name'] == name]
        for seg in segs:
            best_key, best_delta = None, None
            for s in emp_scheds:
                sched_in, sched_out = s['scheduled_in'], s['scheduled_out']
                # Distance to the block: 0 if the login falls inside the block
                if sched_in <= seg['login'] <= sched_out:
                    delta = 0
                else:
                    delta = min(abs((seg['login'] - sched_in).total_seconds()),
                                abs((seg['login'] - sched_out).total_seconds()))
                if delta <= MAX_ASSIGN_HOURS*3600 and (best_delta is None or delta < best_delta):
                    best_delta = delta
                    best_key = (name, str(s['shift_date']), s.get('block_no') or 1)
            if best_key:
                assignment[id(seg)] = best_key

    # 1) Every scheduled shift — matched against actual
    for s in schedules:
        name = s['employee_name']
        sched_in, sched_out = s['scheduled_in'], s['scheduled_out']
        break_allowed = s['break_minutes'] or 0
        sched_key = (name, str(s['shift_date']), s.get('block_no') or 1)

        segments = [seg for seg in shifts_by_emp.get(name, [])
                    if assignment.get(id(seg)) == sched_key]
        segments.sort(key=lambda x: x['login'])
        best = segments[0] if segments else None

        issues = []
        row = {
            'employee_name': name,
            'shift_date': str(s['shift_date']),
            'scheduled_in': sched_in.isoformat(),
            'scheduled_out': sched_out.isoformat(),
            'break_allowed': break_allowed,
            'block_no': s.get('block_no') or 1,
            'from_recurring': s.get('from_recurring', False),
            'scheduled_net_hours': round(((sched_out - sched_in).total_seconds()/3600) - break_allowed/60, 2),
        }

        if not best:
            row.update({'status': 'No Show', 'actual_in': None, 'actual_out': None,
                        'break_taken': 0, 'break_count': 0, 'gross_hours': None,
                        'net_hours': None, 'late_minutes': None, 'early_out_minutes': None,
                        'net_variance': None, 'away_minutes': 0, 'segment_count': 0,
                        'regular_hours': 0, 'overtime_hours': 0, 'regular_pay': 0,
                        'overtime_pay': 0, 'total_pay': 0, 'ot_breakdown': {},
                        'hourly_rate': (rates.get(name) or [{}])[-1].get('hourly_rate', 0),
                        'issues': ['No clock-in found for this scheduled shift']})
            rows.append(row); continue

        # Mark every merged segment as consumed so it isn't double-reported as unscheduled
        for seg in segments:
            matched_shift_keys.add((name, seg['login'].isoformat()))

        # Apply a manager adjustment if one exists for this shift. The raw clocker
        # data is untouched — we clone the segments and override the boundaries,
        # keeping the originals so the report can show both.
        adj = adjustments.get(sched_key)
        original_in = segments[0]['login']
        original_out = segments[-1]['logout']
        original_break = sum(b['minutes'] for seg in segments for b in seg['breaks'])

        if adj:
            import copy
            segments = copy.deepcopy(segments)
            if adj.get('adjusted_in'):
                segments[0]['login'] = adj['adjusted_in']
            if adj.get('adjusted_out'):
                segments[-1]['logout'] = adj['adjusted_out']
            if adj.get('adjusted_break_minutes') is not None:
                target = float(adj['adjusted_break_minutes'])
                current = sum(b['minutes'] for seg in segments for b in seg['breaks'])
                if segments[0]['breaks']:
                    # Apply the difference to the first break so totals match exactly
                    delta = target - current
                    b0 = segments[0]['breaks'][0]
                    b0['minutes'] = max(0, b0['minutes'] + delta)
                    if b0.get('end') and b0.get('start'):
                        b0['end'] = b0['start'] + timedelta(minutes=b0['minutes'])
                elif target > 0:
                    segments[0]['breaks'].append({
                        'start': segments[0]['login'], 'end': segments[0]['login'] + timedelta(minutes=target),
                        'declared_minutes': target, 'minutes': target, 'closed': True})

        login = segments[0]['login']
        logout = segments[-1]['logout']
        break_taken = sum(b['minutes'] for seg in segments for b in seg['breaks'])
        break_declared = sum(b.get('declared_minutes', 0) for seg in segments for b in seg['breaks'])
        break_count = sum(len(seg['breaks']) for seg in segments)

        # Flag breaks that ran longer than the agent said they would
        for seg in segments:
            for b in seg['breaks']:
                dec = b.get('declared_minutes', 0)
                act = b['minutes']
                if dec > 0 and act - dec > grace:
                    issues.append(
                        f"Break ran {act:.0f}m vs {dec:.0f}m declared "
                        f"(+{act-dec:.0f}m, from {b['start'].strftime('%-I:%M %p')})"
                    )
                if not b.get('closed'):
                    issues.append(f"Break at {b['start'].strftime('%-I:%M %p')} never closed — using declared {dec:.0f}m")

        # Time actually clocked in = sum of each segment, so mid-shift gaps don't count
        worked_seconds = 0
        for seg in segments:
            if seg['logout']:
                worked_seconds += (seg['logout'] - seg['login']).total_seconds()

        # Gaps between segments = logged out mid-shift
        away_minutes = 0
        for prev, nxt in zip(segments, segments[1:]):
            if prev['logout']:
                gap = (nxt['login'] - prev['logout']).total_seconds()/60
                if gap > 0:
                    away_minutes += gap
                    issues.append(
                        f"Logged out mid-shift for {gap:.0f} min "
                        f"({prev['logout'].strftime('%-I:%M %p')}–{nxt['login'].strftime('%-I:%M %p')})"
                    )

        late_min = (login - sched_in).total_seconds()/60
        if late_min > grace: issues.append(f'Late login by {late_min:.0f} min')
        elif late_min < -grace: issues.append(f'Early login by {abs(late_min):.0f} min')

        early_out_min = None
        gross = net = None
        if logout:
            early_out_min = (sched_out - logout).total_seconds()/60
            if early_out_min > grace: issues.append(f'Left early by {early_out_min:.0f} min')
            elif early_out_min < -grace: issues.append(f'Stayed late by {abs(early_out_min):.0f} min')
            gross = (logout - login).total_seconds()/3600           # full span of the shift
            net = worked_seconds/3600 - break_taken/60              # actual paid time
        else:
            issues.append('Never clocked out')

        break_over = break_taken - break_allowed
        if break_over > grace: issues.append(f'Break over by {break_over:.0f} min')
        if any(seg.get('partial') for seg in segments):
            issues.append('Login not captured in uploaded report')

        pay = _compute_pay(segments, (sched_in, sched_out), rates.get(name, []), ot_periods)
        if pay['overtime_hours'] > 0:
            mults = ', '.join(f"{h}h @{m}x" for m, h in pay['ot_breakdown'].items())
            issues.append(f"Overtime {pay['overtime_hours']}h ({mults})")

        if adj:
            issues.append(f"⚠️ Times adjusted by {adj.get('adjusted_by_name','manager')}: {adj.get('reason','')}")

        row.update({
            'status': 'OK' if not issues else 'Variance',
            'actual_in': login.isoformat(),
            'actual_out': logout.isoformat() if logout else None,
            'is_adjusted': bool(adj),
            'adjustment_id': adj['id'] if adj else None,
            'adjustment_reason': adj.get('reason') if adj else None,
            'adjusted_by_name': adj.get('adjusted_by_name') if adj else None,
            'adjusted_at': adj['created_at'].isoformat() if adj and adj.get('created_at') else None,
            'original_in': original_in.isoformat() if original_in else None,
            'original_out': original_out.isoformat() if original_out else None,
            'original_break': round(original_break),
            'break_taken': round(break_taken),
            'break_declared': round(break_declared),
            'break_count': break_count,
            'away_minutes': round(away_minutes),
            'segment_count': len(segments),
            'gross_hours': round(gross,2) if gross is not None else None,
            'net_hours': round(net,2) if net is not None else None,
            'late_minutes': round(late_min),
            'early_out_minutes': round(early_out_min) if early_out_min is not None else None,
            'net_variance': round(net - row['scheduled_net_hours'],2) if net is not None else None,
            'issues': issues,
            **pay,
        })
        rows.append(row)

    # 2) Worked shifts with NO matching schedule (unscheduled work)
    for name, shs in shifts_by_emp.items():
        for sh in shs:
            key = (name, sh['login'].isoformat())
            if key in matched_shift_keys: continue
            # Only include if its login date falls inside the requested window
            d = sh['login'].date()
            if date_from and str(d) < date_from: continue
            if date_to and str(d) > date_to: continue
            break_taken = sum(b['minutes'] for b in sh['breaks'])
            gross = net = None
            if sh['logout']:
                gross = (sh['logout'] - sh['login']).total_seconds()/3600
                net = gross - break_taken/60
            # Unscheduled work is entirely overtime by policy
            unsched_pay = _compute_pay([sh], None, rates.get(name, []), ot_periods)
            unsched_issues = ['No schedule entered for this shift'] + (['Never clocked out'] if not sh['logout'] else [])
            if unsched_pay['overtime_hours'] > 0:
                mults = ', '.join(f"{h}h @{m}x" for m, h in unsched_pay['ot_breakdown'].items())
                unsched_issues.append(f"All overtime — {unsched_pay['overtime_hours']}h ({mults})")
            rows.append({
                'employee_name': name, 'shift_date': str(d),
                'scheduled_in': None, 'scheduled_out': None, 'break_allowed': None,
                'block_no': 1, 'from_recurring': False,
                'scheduled_net_hours': None, 'status': 'Unscheduled',
                'actual_in': sh['login'].isoformat(),
                'actual_out': sh['logout'].isoformat() if sh['logout'] else None,
                'break_taken': round(break_taken), 'break_count': len(sh['breaks']),
                'away_minutes': 0, 'segment_count': 1,
                'gross_hours': round(gross,2) if gross is not None else None,
                'net_hours': round(net,2) if net is not None else None,
                'late_minutes': None, 'early_out_minutes': None, 'net_variance': None,
                'issues': unsched_issues,
                **unsched_pay,
            })

    rows.sort(key=lambda r: (r['shift_date'], r['employee_name']), reverse=True)

    # Payroll totals for the whole range, and per agent
    totals = {
        'regular_hours': round(sum(r.get('regular_hours') or 0 for r in rows), 2),
        'overtime_hours': round(sum(r.get('overtime_hours') or 0 for r in rows), 2),
        'regular_pay': round(sum(r.get('regular_pay') or 0 for r in rows), 2),
        'overtime_pay': round(sum(r.get('overtime_pay') or 0 for r in rows), 2),
        'total_pay': round(sum(r.get('total_pay') or 0 for r in rows), 2),
    }
    per_agent = {}
    for r in rows:
        a = per_agent.setdefault(r['employee_name'], {
            'employee_name': r['employee_name'], 'hourly_rate': r.get('hourly_rate', 0),
            'regular_hours': 0, 'overtime_hours': 0, 'regular_pay': 0,
            'overtime_pay': 0, 'total_pay': 0, 'ot_breakdown': {},
        })
        a['regular_hours'] += r.get('regular_hours') or 0
        a['overtime_hours'] += r.get('overtime_hours') or 0
        a['regular_pay'] += r.get('regular_pay') or 0
        a['overtime_pay'] += r.get('overtime_pay') or 0
        a['total_pay'] += r.get('total_pay') or 0
        for m, h in (r.get('ot_breakdown') or {}).items():
            a['ot_breakdown'][m] = round(a['ot_breakdown'].get(m, 0) + h, 2)
    for a in per_agent.values():
        for k in ('regular_hours','overtime_hours','regular_pay','overtime_pay','total_pay'):
            a[k] = round(a[k], 2)

    # ---- phone availability -------------------------------------------
    # Per shift: how much of it the agent was actually reachable — phone online
    # AND CMS active. A shift with no snapshots gets None rather than zero, so
    # "we didn't collect" can never be read as "they did nothing".
    try:
        import phone_status as _PS
        from datetime import datetime as _dtx
        _ext_by_name, _id_by_ext = _phone_identity_map()
        _conn2 = get_db()
        try:
            _interval = _PS.detect_interval(_conn2)
            for _row in rows:
                _row['availability'] = None
                _ai, _ao = _row.get('actual_in'), _row.get('actual_out')
                if not _ai or not _ao:
                    continue
                _ext = _ext_by_name.get(str(_row.get('employee_name') or '').strip().lower())
                _eid = _id_by_ext.get(_ext) if _ext else None
                if not _ext and _eid is None:
                    continue
                _av = _availability_for_shift(
                    _conn2, _ext, _eid,
                    _dtx.fromisoformat(_ai), _dtx.fromisoformat(_ao), _interval)
                _row['availability'] = _av
                if not _av:
                    continue

                # ---- payroll deduction ----------------------------------
                # Any stretch where they couldn't take a call comes off paid
                # time — the slow start after clocking in, the early finish,
                # and anything in the middle of the shift. CMS break is not
                # counted: it's already out of net hours, so deducting it here
                # would charge the same minutes twice.
                #
                # Everything is reported side by side — hours worked, each gap,
                # what was deducted, and the pay before and after — so the
                # figure can always be explained to the agent.
                _grace = float(_phone_grace_minutes())
                _min_gap = float(_phone_min_gap_minutes())
                _late = _av.get('late_to_phone_minutes') or 0.0
                _early = _av.get('early_off_phone_minutes') or 0.0

                _counted, _ignored = [], 0.0
                for _g in (_av.get('gaps') or []):
                    _mins = _g['minutes']
                    _at_start = _g['from'] == _ai or abs(
                        (_dtx.fromisoformat(_g['from']) - _dtx.fromisoformat(_ai)).total_seconds()) < 90
                    # the start gap gets the grace allowance (a phone takes a
                    # moment to register); brief blips anywhere are ignored as
                    # snapshot noise rather than real absence
                    _at_end = abs((_dtx.fromisoformat(_g['to']) -
                                   _dtx.fromisoformat(_ao)).total_seconds()) < 90
                    _charge = max(0.0, _mins - _grace) if _at_start else _mins
                    if _charge <= 0 or _mins < _min_gap:
                        _ignored += _mins
                        continue
                    _where = 'start' if _at_start else ('end' if _at_end else 'middle')
                    _counted.append({**_g, 'deducted_minutes': round(_charge, 1),
                                     'at_shift_start': _at_start, 'where': _where})
                _ded = round(sum(_g['deducted_minutes'] for _g in _counted), 1)

                _net = _row.get('net_hours')
                _rate = float(_row.get('hourly_rate') or 0)
                _orig_pay = _row.get('total_pay')
                _reduction = round(_ded / 60.0 * _rate, 2) if _rate else 0.0
                _row['phone_deduction'] = {
                    'original_net_hours': _net,
                    'original_pay': _orig_pay,
                    'gaps_counted': _counted,
                    'gaps_ignored_minutes': round(_ignored, 1),
                    'late_start_minutes': round(max(0.0, _late - _grace), 1),
                    'early_finish_minutes': round(sum(
                        _g['deducted_minutes'] for _g in _counted if _g['where'] == 'end'), 1),
                    'midshift_minutes': round(sum(
                        _g['deducted_minutes'] for _g in _counted if _g['where'] == 'middle'), 1),
                    'total_minutes': _ded,
                    'grace_minutes': _grace,
                    'min_gap_minutes': _min_gap,
                    'payable_hours': round(max(0.0, (_net or 0) - _ded / 60.0), 2) if _net is not None else None,
                    'pay_reduction': _reduction,
                    'adjusted_pay': round(max(0.0, float(_orig_pay or 0) - _reduction), 2)
                                    if _orig_pay is not None else None,
                }
                if _ded > 0:
                    _row.setdefault('issues', []).append(
                        "Couldn't take calls for %.0f min of this shift (%d gap%s) — %.2fh deducted"
                        % (_ded, len(_counted), '' if len(_counted) == 1 else 's', _ded / 60.0))
                if _av.get('available_pct') is not None and _av['available_pct'] < 60:
                    _row.setdefault('issues', []).append(
                        "Available on the phone only %.0f%% of the shift" % _av['available_pct'])
        finally:
            _conn2.close()
    except Exception as _e:
        print('[time_report] availability unavailable: ' + str(_e)[:160])

    # Roll the phone deductions up per agent and across the whole report, so the
    # payroll table can show what was earned, what came off, and what's payable.
    _tot_ded_min, _tot_ded_pay = 0.0, 0.0
    for r in rows:
        pd = r.get('phone_deduction')
        if not pd:
            continue
        a = per_agent.get(r['employee_name'])
        if a is not None:
            a['deducted_minutes'] = round(a.get('deducted_minutes', 0) + (pd['total_minutes'] or 0), 1)
            a['pay_reduction'] = round(a.get('pay_reduction', 0) + (pd['pay_reduction'] or 0), 2)
            a['adjusted_total_pay'] = round(a['total_pay'] - a['pay_reduction'], 2)
            a['payable_hours'] = round(
                a.get('payable_hours', a['regular_hours'] + a['overtime_hours'])
                - (pd['total_minutes'] or 0) / 60.0, 2)
        _tot_ded_min += pd['total_minutes'] or 0
        _tot_ded_pay += pd['pay_reduction'] or 0
    for a in per_agent.values():
        a.setdefault('deducted_minutes', 0)
        a.setdefault('pay_reduction', 0)
        a.setdefault('adjusted_total_pay', a['total_pay'])
    totals['phone_deducted_minutes'] = round(_tot_ded_min, 1)
    totals['phone_pay_reduction'] = round(_tot_ded_pay, 2)
    totals['adjusted_total_pay'] = round((totals.get('total_pay') or 0) - _tot_ded_pay, 2)

    # Any special overtime rate currently in force
    now = datetime.now()
    active_ot = [p for p in ot_periods if p['starts_at'] <= now and (p['ends_at'] is None or p['ends_at'] >= now)]

    return jsonify({
        'report': rows, 'count': len(rows),
        'totals': totals,
        'per_agent': sorted(per_agent.values(), key=lambda x: x['employee_name']),
        'default_ot_multiplier': DEFAULT_OT_MULTIPLIER,
        'active_ot_periods': [
            {'id': p['id'], 'multiplier': float(p['multiplier']),
             'starts_at': p['starts_at'].isoformat(),
             'ends_at': p['ends_at'].isoformat() if p['ends_at'] else None,
             'note': p.get('note','')}
            for p in active_ot
        ],
    })

@app.route('/api/clocker-employees', methods=['GET'])
@require_manager
def clocker_employees():
    """Distinct employee names seen in uploaded clocker data — for the schedule dropdown."""
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT DISTINCT employee_name FROM clocker_events ORDER BY employee_name')
    names = [r[0] for r in c.fetchall()]
    conn.close()
    return jsonify({'employees': names})

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

# ============================================================================
#  Skin Block asset mirror
#  The agents' browsers sit behind the Techloq content filter, which blocks the
#  outside AI-model CDNs. So VoiceGuard fetches the detector files server-side
#  (Azure is unfiltered), caches them on disk, and serves them from OUR domain,
#  which the agents can already reach. Same idea as the recording relay.
# ============================================================================
def _sb_cache_dir():
    """Where the detector files live between deploys.

    On Azure App Service /home is real storage that survives a restart or a new
    deploy; anywhere else in the container does not. Keeping the cache there is
    the difference between downloading 60 MB once and downloading it again after
    every push — which is why the agents kept dropping back to the slow path.
    """
    for path in (os.getenv('SB_CACHE_DIR'), '/home/sbassets_cache',
                 os.path.join(os.getenv('HOME', '.'), 'sbassets_cache')):
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, '.writable')
            with open(probe, 'w') as fh:
                fh.write('x')
            os.remove(probe)
            return path
        except Exception:
            continue
    return os.path.join('.', 'sbassets_cache')


SB_CACHE = _sb_cache_dir()
# Several mirrors each. If one is unreachable from this data centre the next is
# tried, and the one that worked is reported — a single blocked CDN was enough
# to leave the detector unable to start with no explanation.
SB_LIB_BASES = [
    'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/',
    'https://unpkg.com/onnxruntime-web@1.19.2/dist/',
    'https://fastly.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/',
    'https://gcore.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/',
]
SB_HF_BASES = [
    'https://huggingface.co/',
    'https://hf-mirror.com/',
]
SB_LIB_BASE = SB_LIB_BASES[0]
SB_HF_BASE = SB_HF_BASES[0]
SB_HF_ALLOWED = ('Xenova/segformer_b2_clothes/', 'Xenova/segformer_b0_clothes/')

def _sb_looks_like_model(path):
    """A real ONNX model is protobuf and large. Anything that starts like text
    (JavaScript, HTML, an LFS pointer, an error page) is not the model — serving
    it silently is what made the browser fall back to the server."""
    try:
        if os.path.getsize(path) < 1024 * 1024:
            return False, 'file is only %d bytes' % os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(64)
        for bad in (b'/*', b'<!', b'<htm', b'<?xm', b'var ', b'"use', b'{', b'version http'):
            if head.lstrip()[:len(bad)].lower() == bad.lower():
                return False, 'content starts with %r — not a model' % head[:24]
        return True, ''
    except Exception as e:
        return False, str(e)[:120]


def _sb_fetch(cache_key, url, expect_model=False):
    """Download once, then serve from disk (App Service /home persists).

    Library files and model files are cached in SEPARATE folders so a name can
    never collide, and a model is validated before it is kept — a bad download
    is deleted rather than cached and served forever.
    """
    sub = 'model' if expect_model else 'lib'
    folder = os.path.join(SB_CACHE, sub)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', cache_key)
    path = os.path.join(folder, safe)

    if os.path.exists(path) and expect_model:
        ok, why = _sb_looks_like_model(path)
        if not ok:
            print('[skinblock] cached model was bad (%s) — refetching' % why)
            try:
                os.remove(path)
            except Exception:
                pass

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        tmp = path + '.part.' + str(os.getpid())
        urls = url if isinstance(url, list) else [url]
        errors = []
        got = False
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={'User-Agent': 'VoiceGuard-SkinBlock/1.0'})
                with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, 'wb') as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                got = True
                print('[skinblock] mirrored %s from %s' % (safe, u.split('/')[2]))
                break
            except Exception as e:
                errors.append('%s: %s' % (u.split('/')[2], str(e)[:120]))
                continue
        if not got:
            raise RuntimeError('could not download from any source — ' + ' | '.join(errors))
        if expect_model:
            ok, why = _sb_looks_like_model(tmp)
            if not ok:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise RuntimeError('downloaded file is not a model: %s (from %s)' % (why, url))
        os.replace(tmp, path)
    return path

@app.route('/sbassets/warmup', methods=['GET'])
def sb_warmup():
    """Download everything the detector needs, now, and report each file.

    The browser asks for these one at a time and gives up quietly if any of them
    fails. Doing it here means one page tells you exactly which file could not be
    fetched and why — and after a successful run the agents' browsers get
    everything from our own disk.
    """
    wanted = [
        ('lib', 'ort.min.js'),
        ('lib', 'ort-wasm-simd-threaded.jsep.mjs'),
        ('lib', 'ort-wasm-simd-threaded.jsep.wasm'),
        ('lib', 'ort-wasm-simd-threaded.mjs'),
        ('lib', 'ort-wasm-simd-threaded.wasm'),
        ('model', 'Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx'),
    ]
    out, ok_count = [], 0
    for kind_, name in wanted:
        entry = {'file': name, 'kind': kind_}
        try:
            if kind_ == 'lib':
                p = _sb_fetch('lib_' + name, [b + name for b in SB_LIB_BASES])
            else:
                p = _sb_fetch('hf_' + name, [b + name for b in SB_HF_BASES], expect_model=True)
            entry['ok'] = True
            entry['bytes'] = os.path.getsize(p)
            entry['size'] = '%.1f MB' % (entry['bytes'] / 1048576.0)
            ok_count += 1
        except Exception as e:
            entry['ok'] = False
            entry['error'] = str(e)[:400]
        out.append(entry)
    essential = [e for e in out if e['file'] in ('ort.min.js',) or e['kind'] == 'model']
    return jsonify({
        'files': out,
        'downloaded': ok_count,
        'ready': all(e.get('ok') for e in essential),
        'meaning': ('Everything needed is on the server — reload the Skin Block page.'
                    if all(e.get('ok') for e in essential)
                    else 'Something could not be downloaded; see the error next to each file.'),
    })


@app.route('/sbassets/status', methods=['GET'])
def sb_assets_status():
    """What the mirror actually has on disk — size and first bytes of each file.
    Open this to see at a glance whether the model cached correctly."""
    out = []
    for sub in ('lib', 'model'):
        folder = os.path.join(SB_CACHE, sub)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            p = os.path.join(folder, name)
            try:
                size = os.path.getsize(p)
                with open(p, 'rb') as f:
                    head = f.read(16)
                entry = {'kind': sub, 'file': name, 'bytes': size,
                         'head': head.hex(), 'head_text': head.decode('latin-1', 'replace')}
                if sub == 'model':
                    ok, why = _sb_looks_like_model(p)
                    entry['valid_model'] = ok
                    if not ok:
                        entry['problem'] = why
                out.append(entry)
            except Exception as e:
                out.append({'kind': sub, 'file': name, 'error': str(e)[:120]})
    return jsonify({'cache_dir': SB_CACHE, 'files': out})


@app.route('/sbassets/clear-cache', methods=['POST', 'GET'])
@require_manager
def sb_assets_clear():
    """Drop the mirrored files so the next request downloads them fresh."""
    removed = []
    for sub in ('lib', 'model'):
        folder = os.path.join(SB_CACHE, sub)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
                removed.append(sub + '/' + name)
            except Exception:
                pass
    return jsonify({'removed': removed})


@app.route('/sbassets/lib/<path:fname>')
def sb_lib(fname):
    """Serves the ONNX Runtime Web library and its .wasm workers, from our own
    domain, so the agents' browsers never touch an outside CDN."""
    if not re.fullmatch(r'[A-Za-z0-9._-]+', fname):
        return jsonify({'error': 'bad name'}), 400
    try:
        p = _sb_fetch('lib_' + fname, [b + fname for b in SB_LIB_BASES])
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 502
    mime = ('application/wasm' if fname.endswith('.wasm')
            else 'text/javascript' if fname.endswith(('.js', '.mjs'))
            else 'application/octet-stream')
    r = make_response(send_file(p, mimetype=mime))
    r.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    r.headers['Cache-Control'] = 'public, max-age=31536000'
    return r

@app.route('/sbassets/hf/<path:hfpath>')
def sb_hf(hfpath):
    """Serves the detector model files. Locked to the two approved models only —
    this is a mirror for Skin Block, not an open proxy."""
    if '..' in hfpath or not hfpath.startswith(SB_HF_ALLOWED):
        return jsonify({'error': 'not allowed'}), 403
    try:
        p = _sb_fetch('hf_' + hfpath, [b + hfpath for b in SB_HF_BASES], expect_model=True)
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 502
    mime = 'application/json' if hfpath.endswith('.json') else 'application/octet-stream'
    r = make_response(send_file(p, mimetype=mime))
    r.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    r.headers['Cache-Control'] = 'public, max-age=31536000'
    return r

@app.route('/skinblock')
def skinblock_page():
    """Public — no login required so any agent can use it directly.

    The two Cross-Origin headers below switch the browser into 'isolated' mode,
    which is what unlocks multi-threaded WASM — the detector then uses all of
    the PC's cores instead of one. Everything the page loads is same-origin, so
    nothing breaks.
    """
    resp = make_response(send_from_directory('.', 'skinblock.html'))
    resp.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    resp.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    return resp

# Hostnames that serve ONLY the CMS side. Anything else on these addresses is
# refused, so this stays a separate system from the QA dashboard even though
# one application serves both.
SERVER_BUILD = 'portal-hosts-2'      # bump when you need to confirm a deploy landed

PORTAL_HOSTS = [hh.strip().lower() for hh in
                os.getenv('PORTAL_HOSTS',
                          'cms.myhellodesk.com,proclick.myhellodesk.com').split(',')
                if hh.strip()]


def _is_portal_host():
    host = (request.host or '').split(':')[0].lower()
    return host in PORTAL_HOSTS


# What the CMS address is allowed to reach. Everything else there is a 404 —
# the QA dashboard, the Skin Block tool and the QA APIs are simply not part of
# this system.
PORTAL_ALLOWED_PREFIXES = (
    '/api/whoami', '/api/portal/', '/api/live', '/api/customers', '/api/agents',
    '/api/recording-link', '/api/payments/', '/api/cms-settings',
    '/api/cms-db/', '/api/connections', '/static/', '/favicon',
)


@app.before_request
def _portal_host_guard():
    """On the CMS address, serve the portal and nothing else."""
    if not _is_portal_host():
        return None
    p = request.path
    if p in ('/', '/portal', '/portal/'):
        return None
    if p.startswith(PORTAL_ALLOWED_PREFIXES):
        return None
    # not part of this system
    return jsonify({'error': 'Not available on this address'}), 404


@app.route('/api/whoami')
def whoami():
    """What address the server thinks it is answering on.

    If the CMS login isn't appearing, this says whether the hostname reached the
    server as expected and whether it is on the recognised list — which
    separates a DNS or deploy problem from a browser cache.
    """
    host = (request.host or '').split(':')[0].lower()
    return jsonify({
        'host_seen_by_server': host,
        'recognised_cms_hosts': PORTAL_HOSTS,
        'is_cms_address': _is_portal_host(),
        'serves': 'the CMS portal' if _is_portal_host() else 'the QA dashboard',
        'version': SERVER_BUILD,
    })


@app.route('/')
def index():
    """The QA dashboard normally; the CMS portal on its own hostname.

    The page is never cached. It changes with every deploy, and a browser
    holding yesterday's copy shows the wrong login entirely — which is exactly
    what happened on the CMS address.
    """
    if _is_portal_host():
        return portal_page()
    resp = make_response(send_from_directory('.', 'qa-dashboard.html'))
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

try:
    init_db()
except Exception as e:
    print(f'⚠️ DB init warning: {e}')


def _warm_skinblock_assets():
    """Fetch the detector files once at startup if they aren't already here.

    Runs in the background so it never delays the app coming up. If the cache
    survived the deploy this does nothing."""
    import threading

    def run():
        try:
            wanted = [('lib', 'ort.min.js'),
                      ('lib', 'ort-wasm-simd-threaded.jsep.mjs'),
                      ('lib', 'ort-wasm-simd-threaded.jsep.wasm'),
                      ('lib', 'ort-wasm-simd-threaded.mjs'),
                      ('lib', 'ort-wasm-simd-threaded.wasm'),
                      ('model', 'Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx')]
            have = 0
            for kind_, name in wanted:
                try:
                    if kind_ == 'lib':
                        _sb_fetch('lib_' + name, [b + name for b in SB_LIB_BASES])
                    else:
                        _sb_fetch('hf_' + name, [b + name for b in SB_HF_BASES], expect_model=True)
                    have += 1
                except Exception as e:
                    print('[skinblock] warm-up could not get %s: %s' % (name, str(e)[:140]))
            print('[skinblock] detector files ready: %d of %d in %s' % (have, len(wanted), SB_CACHE))
        except Exception as e:
            print('[skinblock] warm-up failed: ' + str(e)[:160])

    threading.Thread(target=run, daemon=True).start()


_warm_skinblock_assets()

if __name__ == '__main__':
    print('\n✅ VoiceGuard QA Server running!')
    app.run(debug=True, port=5000)
