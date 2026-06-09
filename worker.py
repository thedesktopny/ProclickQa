"""
VoiceGuard Background Worker
Processes call analysis jobs from the Redis queue.
Run this alongside the Flask server on Azure.
"""
import os
import json
import time
import redis
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import urllib.request
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [WORKER] %(message)s')
log = logging.getLogger(__name__)

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
DATABASE_URL = os.getenv('DATABASE_URL', '')
QUEUE_NAME = 'voiceguard:calls'
UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)

def update_call_status(call_id, status, error=None):
    """Update call status in database."""
    conn = get_db()
    c = conn.cursor()
    if error:
        c.execute(
            "UPDATE calls SET status=%s, scorecard=%s WHERE call_id=%s",
            (status, json.dumps({'error': error}), call_id)
        )
    else:
        c.execute("UPDATE calls SET status=%s WHERE call_id=%s", (status, call_id))
    conn.commit()
    conn.close()

def insert_call_record(call_id, agent_name, agent_extension, recording_url, call_duration_seconds=0, billed_minutes=0):
    """Insert a pending call record immediately when job is received."""
    mins = call_duration_seconds // 60 if call_duration_seconds else 0
    secs = call_duration_seconds % 60 if call_duration_seconds else 0
    duration_display = f"{mins}:{secs:02d}" if call_duration_seconds else '--'
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO calls (call_id, agent_name, agent_extension, recording_url, 
                         call_duration_seconds, billed_minutes, duration, status, overall_score, flags)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    ''', (call_id, agent_name, agent_extension, recording_url,
          call_duration_seconds, billed_minutes, duration_display, 'Processing', 0, 0))
    conn.commit()
    conn.close()

def process_job(job_data):
    """Process a single call analysis job."""
    call_id = job_data.get('call_id')
    agent_name = job_data.get('agent_name')
    agent_extension = job_data.get('agent_extension', '')
    recording_url = job_data.get('recording_url')
    call_duration_seconds = job_data.get('call_duration_seconds', 0)
    billed_minutes = job_data.get('billed_minutes', 0)
    caller_id = job_data.get('caller_id', '')
    call_dropped = job_data.get('call_dropped', False)

    log.info(f"Processing call {call_id} for agent {agent_name}")

    # Detect extension
    url_path = recording_url.split('?')[0]
    ext = os.path.splitext(url_path)[1].lower()
    if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
        ext = '.wav'

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    audio_path = os.path.join(UPLOAD_DIR, f"call_{call_id}_{int(time.time())}{ext}")

    try:
        # Download audio
        log.info(f"Downloading audio from {recording_url}")
        urllib.request.urlretrieve(recording_url, audio_path)

        # Run AI analysis
        log.info(f"Running AI analysis...")
        from ai_engine import analyze_call as run_analysis
        result = run_analysis(audio_path, agent_name, call_id, caller_id=caller_id, call_dropped=call_dropped)

        # Save full results to Postgres
        conn = get_db()
        c = conn.cursor()

        c.execute('''
            UPDATE calls SET
                duration=%s, overall_score=%s, confidence=%s, emotion=%s, status=%s,
                flags=%s, scorecard=%s, transcript=%s, summary=%s,
                agent_extension=%s, emotion_delta=%s, requires_human_review=%s,
                human_review_reason=%s, age_concern=%s, coaching_notes=%s,
                call_dropped=%s, callback_made=%s, callback_call_id=%s
            WHERE call_id=%s
        ''', (
            result.get('duration', '--'),
            result['overall_score'],
            result.get('confidence', 100),
            result['emotion'],
            result['status'],
            result['flags'],
            json.dumps(result.get('scorecard', {})),
            result.get('transcript', ''),
            result.get('summary', ''),
            agent_extension,
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

        # Save per-rule results
        scorecard = result.get('scorecard', {})
        rules_evaluation = scorecard.get('rules_evaluation', [])
        for rule_result in rules_evaluation:
            c.execute('''
                INSERT INTO rule_results (call_id, rule_description, category, severity, passed, evidence)
                VALUES (%s,%s,%s,%s,%s,%s)
            ''', (
                call_id,
                rule_result.get('rule', ''),
                rule_result.get('category', ''),
                rule_result.get('severity', ''),
                rule_result.get('passed', False),
                rule_result.get('evidence', '')
            ))

        conn.commit()
        conn.close()

        log.info(f"✅ Call {call_id} scored: {result['overall_score']}% — {result['status']}")
        return True

    except Exception as e:
        log.error(f"❌ Failed to process call {call_id}: {str(e)}")
        update_call_status(call_id, 'Failed', str(e))
        return False

    finally:
        try:
            os.remove(audio_path)
        except:
            pass

def run_worker():
    """Main worker loop — processes jobs from Redis queue."""
    log.info("🚀 VoiceGuard Worker starting...")
    r = get_redis()
    log.info(f"✅ Connected to Redis")
    log.info(f"👂 Listening on queue: {QUEUE_NAME}")

    retry_counts = {}

    while True:
        try:
            # Block and wait for next job (timeout 30s)
            job = r.brpop(QUEUE_NAME, timeout=30)

            if job is None:
                continue

            _, job_json = job
            job_data = json.loads(job_json)
            call_id = job_data.get('call_id', 'unknown')

            # Track retries
            retry_counts[call_id] = retry_counts.get(call_id, 0) + 1

            success = process_job(job_data)

            if not success and retry_counts[call_id] < 3:
                # Retry failed jobs up to 3 times with delay
                log.info(f"Retrying call {call_id} (attempt {retry_counts[call_id]}/3)")
                time.sleep(30)
                r.lpush(QUEUE_NAME, job_json)
            elif retry_counts[call_id] >= 3:
                log.error(f"Call {call_id} failed after 3 attempts — giving up")
                del retry_counts[call_id]
            else:
                retry_counts.pop(call_id, None)

        except redis.RedisError as e:
            log.error(f"Redis error: {e} — reconnecting in 5s")
            time.sleep(5)
        except Exception as e:
            log.error(f"Worker error: {e}")
            time.sleep(5)

if __name__ == '__main__':
    run_worker()
