"""
VoiceGuard Background Worker
Processes call analysis jobs from Redis queue.
"""
import os
import json
import time
import urllib.request
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [WORKER] %(message)s')
log = logging.getLogger(__name__)

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
DATABASE_URL = os.getenv('DATABASE_URL', '')
QUEUE_NAME = 'voiceguard:calls'
UPLOAD_DIR = os.path.join(os.getenv('HOME', '.'), 'uploads')

def get_db():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def get_redis():
    import redis
    return redis.from_url(REDIS_URL, decode_responses=True)

def process_job(job_data):
    call_id = job_data.get('call_id')
    agent_name = job_data.get('agent_name')
    agent_extension = job_data.get('agent_extension', '')
    recording_url = job_data.get('recording_url')
    call_duration_seconds = job_data.get('call_duration_seconds', 0)
    billed_minutes = job_data.get('billed_minutes', 0)
    caller_id = job_data.get('caller_id', '')
    customer_account_id = job_data.get('customer_account_id', '')
    account_name = job_data.get('account_name', '')
    call_end_first = job_data.get('call_end_first', 'customer')
    agent_qos_tx = job_data.get('agent_qos_tx', 'Good')
    agent_qos_rx = job_data.get('agent_qos_rx', 'Good')
    customer_qos_tx = job_data.get('customer_qos_tx', 'Good')
    customer_qos_rx = job_data.get('customer_qos_rx', 'Good')
    call_notes = job_data.get('call_notes', '')
    call_dropped = job_data.get('call_dropped', False)

    log.info(f"Processing call {call_id} — {agent_name}")

    url_path = recording_url.split('?')[0]
    ext = os.path.splitext(url_path)[1].lower()
    if ext not in ['.mp3', '.wav', '.m4a', '.ogg', '.webm', '.flac']:
        ext = '.wav'

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    audio_path = os.path.join(UPLOAD_DIR, f"call_{call_id}_{int(time.time())}{ext}")

    try:
        log.info(f"Downloading audio...")
        urllib.request.urlretrieve(recording_url, audio_path)

        log.info(f"Running AI analysis...")
        from ai_engine import analyze_call as run_analysis
        result = run_analysis(
            audio_path, agent_name, call_id,
            call_end_first=call_end_first,
            call_notes=call_notes,
            account_name=account_name,
            agent_qos_tx=agent_qos_tx, agent_qos_rx=agent_qos_rx,
            customer_qos_tx=customer_qos_tx, customer_qos_rx=customer_qos_rx,
            call_dropped=call_dropped
        )

        conn = get_db()
        c = conn.cursor()
        c.execute('''
            UPDATE calls SET
                duration=%s, overall_score=%s, confidence=%s, emotion=%s,
                status=%s, flags=%s, scorecard=%s, transcript=%s, summary=%s,
                agent_extension=%s, emotion_delta=%s, requires_human_review=%s,
                human_review_reason=%s, age_concern=%s, coaching_notes=%s,
                positive_highlights=%s, call_dropped=%s, notes_score=%s,
                notes_feedback=%s
            WHERE call_id=%s
        ''', (
            result.get('duration','--'), result['overall_score'],
            result.get('confidence',100), result['emotion'], result['status'],
            result['flags'], json.dumps(result.get('scorecard',{})),
            result.get('transcript',''), result.get('summary',''),
            agent_extension, json.dumps(result.get('emotion_delta',{})),
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
        log.info(f"✅ Call {call_id} complete — {result['overall_score']}% | {result['status']}")
        return True

    except Exception as e:
        log.error(f"❌ Failed: {call_id} — {str(e)}")
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE calls SET status='Failed', scorecard=%s WHERE call_id=%s",
                      (json.dumps({'error': str(e)}), call_id))
            conn.commit()
            conn.close()
        except: pass
        return False

    finally:
        try: os.remove(audio_path)
        except: pass

def run_worker():
    log.info("🚀 VoiceGuard Worker starting...")
    r = get_redis()
    log.info(f"✅ Redis connected | Queue: {QUEUE_NAME}")
    retry_counts = {}

    while True:
        try:
            job = r.brpop(QUEUE_NAME, timeout=30)
            if job is None:
                continue

            _, job_json = job
            job_data = json.loads(job_json)
            call_id = job_data.get('call_id', 'unknown')
            retry_counts[call_id] = retry_counts.get(call_id, 0) + 1

            success = process_job(job_data)

            if not success and retry_counts[call_id] < 3:
                log.info(f"Retrying {call_id} ({retry_counts[call_id]}/3)...")
                time.sleep(30)
                r.lpush(QUEUE_NAME, job_json)
            else:
                retry_counts.pop(call_id, None)

        except Exception as e:
            log.error(f"Worker error: {e}")
            time.sleep(5)

if __name__ == '__main__':
    run_worker()
