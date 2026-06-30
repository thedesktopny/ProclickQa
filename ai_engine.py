import os
import json
import re
import time
import base64
import requests
import anthropic
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

anthropic_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

DATABASE_URL = os.getenv('DATABASE_URL', '')

def get_db():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    import sqlite3
    return sqlite3.connect(os.path.join(os.getenv('HOME', '.'), 'voiceguard.db'))

# ─── USAGE TRACKING ───────────────────────────────────────────────────────────
# Claude Sonnet 4 pricing (per million tokens)
CLAUDE_INPUT_COST_PER_M = 3.00
CLAUDE_OUTPUT_COST_PER_M = 15.00

# Gemini 2.5 Flash pricing
GEMINI_AUDIO_COST_PER_MIN = 0.001  # $0.001 per audio minute
GEMINI_OUTPUT_COST_PER_M = 3.50    # $3.50 per million output tokens

def track_usage(service, call_id, input_tokens=0, output_tokens=0, audio_seconds=0, cost_usd=0):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO api_usage
            (service, call_id, input_tokens, output_tokens, audio_seconds, cost_usd, used_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())''',
            (service, call_id, input_tokens, output_tokens, audio_seconds, cost_usd))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Usage] Warning: could not track usage: {e}")

# ─── CREDENTIAL REDACTION ─────────────────────────────────────────────────────
def redact_credentials(text):
    if not text:
        return text
    text = re.sub(r'(?i)(password[:\s]+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(my password is[:\s]+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(code[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(verification[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(otp[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'\b\d{6}\b', '[OTP-REDACTED]', text)
    text = re.sub(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', '[CARD-REDACTED]', text)
    text = re.sub(r'\b\d{3}[-]\d{2}[-]\d{4}\b', '[SSN-REDACTED]', text)
    return text

# ─── LOAD RULES ───────────────────────────────────────────────────────────────
def load_active_rules():
    try:
        conn = get_db()
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            c = conn.cursor(cursor_factory=RealDictCursor)
        else:
            import sqlite3
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute("SELECT description, category, severity FROM rules WHERE active=1 ORDER BY severity DESC")
        rules = [dict(r) for r in c.fetchall()]
        conn.close()
        return rules
    except Exception as e:
        print(f"[Warning] Could not load rules: {e}")
        return []

# ─── GEMINI AUDIO ANALYSIS ────────────────────────────────────────────────────
def analyze_audio_with_gemini(audio_path):
    print(f"[Gemini] Analyzing: {audio_path}")
    model = genai.GenerativeModel('gemini-2.5-flash')

    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {'.mp3':'audio/mp3','.wav':'audio/wav','.m4a':'audio/mp4',
                '.ogg':'audio/ogg','.flac':'audio/flac','.webm':'audio/webm'}
    mime_type = mime_map.get(ext, 'audio/wav')

    INLINE_SIZE_LIMIT_MB = 18  # Gemini's safe inline limit is ~20MB; use File API above this

    audio_size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Gemini] File size: {round(audio_size_mb, 1)} MB")

    if audio_size_mb <= INLINE_SIZE_LIMIT_MB:
        # Small file — send inline as base64 (fast, no upload needed)
        with open(audio_path, 'rb') as f:
            audio_data = f.read()
        audio_b64 = base64.b64encode(audio_data).decode('utf-8')
        audio_part = {'mime_type': mime_type, 'data': audio_b64}
    else:
        # Large file — upload via Gemini File API using direct REST calls.
        # NOTE: genai.upload_file()/get_file() in the SDK can hit an internal discovery/auth
        # path that doesn't reliably use our configured API key in server environments —
        # confirmed via a real API_KEY_INVALID error during testing. Direct REST calls
        # guarantee the key is sent exactly as provided.
        print(f"[Gemini] File too large for inline ({round(audio_size_mb,1)}MB > {INLINE_SIZE_LIMIT_MB}MB), uploading via File API (REST)...")
        api_key = os.getenv('GEMINI_API_KEY')
        file_size = os.path.getsize(audio_path)

        # Step 1: Start resumable upload session
        start_resp = requests.post(
            f'https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}',
            headers={
                'X-Goog-Upload-Protocol': 'resumable',
                'X-Goog-Upload-Command': 'start',
                'X-Goog-Upload-Header-Content-Length': str(file_size),
                'X-Goog-Upload-Header-Content-Type': mime_type,
                'Content-Type': 'application/json',
            },
            json={'file': {'display_name': os.path.basename(audio_path)}},
            timeout=30
        )
        start_resp.raise_for_status()
        upload_url = start_resp.headers.get('X-Goog-Upload-URL')
        if not upload_url:
            raise Exception(f'Gemini File API did not return an upload URL: {start_resp.text[:200]}')

        # Step 2: Upload the actual file bytes
        with open(audio_path, 'rb') as f:
            file_data = f.read()
        upload_resp = requests.post(
            upload_url,
            headers={
                'X-Goog-Upload-Offset': '0',
                'X-Goog-Upload-Command': 'upload, finalize',
                'Content-Length': str(file_size),
            },
            data=file_data,
            timeout=120
        )
        upload_resp.raise_for_status()
        file_info = upload_resp.json().get('file', {})
        file_uri = file_info.get('uri')
        file_name = file_info.get('name')
        file_state = file_info.get('state', 'PROCESSING')

        if not file_uri:
            raise Exception(f'Gemini File API upload did not return a file URI: {upload_resp.text[:200]}')

        # Step 3: Poll until file becomes ACTIVE (usually instant for audio)
        for _ in range(15):
            if file_state == 'ACTIVE':
                break
            check_resp = requests.get(
                f'https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}',
                timeout=15
            )
            check_resp.raise_for_status()
            file_state = check_resp.json().get('state', 'PROCESSING')
            if file_state == 'ACTIVE':
                break
            time.sleep(2)

        if file_state != 'ACTIVE':
            raise Exception(f'Gemini File API upload never became ACTIVE (stuck at {file_state})')

        audio_part = {'file_data': {'mime_type': mime_type, 'file_uri': file_uri}}
        uploaded_file_name = file_name  # remembered for cleanup after analysis
        print(f"[Gemini] File uploaded and ACTIVE: {file_name}")

    prompt = """You are a call center audio analyst. Listen to this call carefully, with special attention to WHEN things happen (exact timestamps).

Respond ONLY with valid JSON — keep all text values SHORT (max 100 chars each) to avoid truncation:

{
  "transcript": "Summarized transcript max 3000 chars. Label each line AGENT: or CUSTOMER:",
  "duration_estimate": "e.g. 5:32",
  "audio_quality": {
    "overall": "Good/Fair/Poor",
    "background_noise": true/false,
    "background_noise_description": "brief",
    "audio_cuts_or_drops": true/false
  },
  "emotion_analysis": {
    "customer_emotion_start": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_end": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_overall": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_frustration_level": 0,
    "customer_satisfaction_level": 0,
    "customer_repeated_themselves": true/false,
    "customer_repeated_count": 0,
    "customer_sounds_young": true/false,
    "customer_raised_voice": true/false,
    "agent_tone": "Professional/Friendly/Neutral/Rushed/Defensive/Robotic/Rude",
    "agent_stress_level": 0,
    "agent_speaking_speed": "Too Fast/Normal/Too Slow",
    "agent_talk_ratio": 50,
    "agent_interrupted_customer": true/false,
    "long_silences_detected": true/false,
    "longest_silence_seconds": 0,
    "key_emotional_moments": [{"timestamp": "2:14", "description": "brief description"}]
  },
  "call_dropped_audio": true/false,
  "summary": "2-3 sentence summary max",
  "agent_uncertainty_phrases": ["phrase1", "phrase2"],
  "explicit_content_detected": true/false,
  "explicit_content_description": "",
  "flagged_moments": [
    {
      "timestamp_seconds": 0,
      "timestamp_display": "mm:ss",
      "category": "explicit_content/policy_violation/compliance_failure/rudeness/no_verification/no_price_confirmation/no_logout_confirmation/outside_work_solicitation/minor_concern/ignored_instruction/unanswered_question/call_drop_no_callback/wrong_region_currency/other",
      "speaker": "AGENT/CUSTOMER",
      "quote": "exact or near-exact words spoken, max 80 chars",
      "severity": "Critical/Warning",
      "description": "what happened, max 80 chars"
    }
  ]
}

IMPORTANT for flagged_moments: only include moments that are CONCRETE, VERIFIABLE, and TIED TO A SPECIFIC POINT in the audio — not general impressions about the whole call. Each entry must have an accurate timestamp_seconds (total seconds from call start) so we can jump directly to that exact second in playback. Include things like:
- explicit/sexual language by either party
- agent discussing outside work
- agent failing to verify identity/price/logout at the specific moment that should have happened
- rude or unprofessional remarks
- policy violations
- ignored_instruction: customer explicitly says something was already provided/saved/discussed (e.g. "I already gave you that," "it's saved on the account," "I told you this already") and the agent proceeds as if starting fresh anyway
- unanswered_question: customer asks a direct, specific question and the agent's next response never actually answers it (changes subject, answers a different question, or ignores it)
- call_drop_no_callback: the exact moment the call disconnects unexpectedly, especially if the issue was unresolved and no callback was arranged
- wrong_region_currency: agent states a currency, region, or country that contradicts what the customer stated or what's implied by their account/context
Do NOT include vague category-level issues (e.g. "agent seemed unprofessional overall") — only specific moments with an exact timestamp and quote. If nothing flag-worthy occurred, return an empty array."""



    response = model.generate_content(
        [audio_part, prompt],
        generation_config=genai.GenerationConfig(
            max_output_tokens=8192,
            temperature=0.1
        )
    )

    # Clean up uploaded file if we used the File API (avoid storage buildup)
    if audio_size_mb > INLINE_SIZE_LIMIT_MB:
        try:
            api_key = os.getenv('GEMINI_API_KEY')
            requests.delete(
                f'https://generativelanguage.googleapis.com/v1beta/{uploaded_file_name}?key={api_key}',
                timeout=15
            )
            print(f"[Gemini] Cleaned up uploaded file: {uploaded_file_name}")
        except Exception as e:
            print(f"[Gemini] Could not delete uploaded file: {e}")

    text = response.text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
            else:
                raise ValueError("No JSON found")
        except Exception:
            print(f"[Gemini] Warning: Could not parse response, using fallback")
            result = {
                "transcript": "Audio too large or corrupted — manual review needed.",
                "duration_estimate": "--",
                "audio_quality": {"overall": "Unknown", "background_noise": False, "audio_cuts_or_drops": False},
                "emotion_analysis": {
                    "customer_emotion_start": "Neutral", "customer_emotion_end": "Neutral",
                    "customer_emotion_overall": "Neutral", "customer_frustration_level": 0,
                    "customer_satisfaction_level": 50, "customer_repeated_themselves": False,
                    "customer_repeated_count": 0, "customer_sounds_young": False,
                    "customer_raised_voice": False, "agent_tone": "Unknown",
                    "agent_stress_level": 0, "agent_speaking_speed": "Normal",
                    "agent_talk_ratio": 50, "agent_interrupted_customer": False,
                    "long_silences_detected": False, "longest_silence_seconds": 0,
                    "key_emotional_moments": []
                },
                "call_dropped_audio": False,
                "summary": "Audio analysis could not be completed — file may be too large.",
                "agent_uncertainty_phrases": [],
                "explicit_content_detected": False,
                "explicit_content_description": "",
                "flagged_moments": []
            }
    print(f"[Gemini] Done. Emotion: {result['emotion_analysis']['customer_emotion_overall']}")

    # Defensive: ensure flagged_moments always exists even if Gemini omits it
    if 'flagged_moments' not in result:
        result['flagged_moments'] = []

    # Track usage
    try:
        audio_size_mb = os.path.getsize(audio_path) / 1024 / 1024
        # Estimate audio minutes from file size (roughly 1MB/min for WAV)
        estimated_mins = max(1, round(audio_size_mb))
        output_tokens = len(json.dumps(result)) // 4  # rough token estimate
        gemini_cost = (estimated_mins * GEMINI_AUDIO_COST_PER_MIN) + (output_tokens / 1_000_000 * GEMINI_OUTPUT_COST_PER_M)
        track_usage('gemini', None, 0, output_tokens, estimated_mins * 60, round(gemini_cost, 6))
    except Exception as e:
        print(f"[Gemini] Usage tracking skipped: {e}")

    return result

# ─── CLAUDE QA SCORING ────────────────────────────────────────────────────────
def score_call_with_claude(gemini_result, rules, call_end_first='customer',
                            call_notes='', account_name='', agent_qos_tx='Good', agent_qos_rx='Good',
                            customer_qos_tx='Good', customer_qos_rx='Good'):
    print(f"[Claude] Scoring against {len(rules)} rules...")

    transcript = gemini_result.get('transcript', '')
    emotion = gemini_result.get('emotion_analysis', {})
    audio_quality = gemini_result.get('audio_quality', {})
    summary = gemini_result.get('summary', '')
    uncertainty_phrases = gemini_result.get('agent_uncertainty_phrases', [])
    explicit_detected = gemini_result.get('explicit_content_detected', False)
    flagged_moments = gemini_result.get('flagged_moments', [])

    flagged_moments_text = ''
    if flagged_moments:
        flagged_moments_text = '\n'.join([
            f"- [{m.get('timestamp_display','?')} / {m.get('timestamp_seconds',0)}s] {m.get('speaker','?')}: \"{m.get('quote','')}\" — {m.get('category','')} ({m.get('severity','Warning')})"
            for m in flagged_moments
        ])

    rules_text = '\n'.join([
        f"- [{r['severity'].upper()}] ({r['category']}) {r['description']}"
        for r in rules
    ])

    # Build call context
    call_end_context = {
        'agent': 'AGENT ended the call first — evaluate if call was fully resolved before hangup',
        'customer': 'Customer ended the call normally',
        'drop': 'Call DROPPED unexpectedly — evaluate if agent attempted callback'
    }.get(call_end_first, 'Unknown')

    qos_context = f'Agent TX (upload): {agent_qos_tx} | Agent RX (download): {agent_qos_rx} | Customer TX: {customer_qos_tx} | Customer RX: {customer_qos_rx}'
    if agent_qos_tx in ['Poor','Fair'] or agent_qos_rx in ['Poor','Fair']:
        qos_context += ' — Agent had connection issues, factor into audio/technical score'
    if customer_qos_tx in ['Poor','Fair'] or customer_qos_rx in ['Poor','Fair']:
        qos_context += ' — Customer had connection issues, reduce agent responsibility for audio quality'

    notes_context = f'Agent call notes: "{call_notes}"' if call_notes else 'Agent wrote NO call notes after this call'
    customer_context = f'Customer: {account_name}' if account_name else ''

    # Cap transcript to prevent oversized prompts
    transcript_capped = transcript[:2500] if len(transcript) > 2500 else transcript

    prompt = f"""You are a call center QA analyst. Score this call strictly and concisely.

TRANSCRIPT (summarized):
{transcript_capped}

METADATA:
{customer_context} | Ended by: {call_end_first} | {notes_context}
Agent QoS: TX={agent_qos_tx} RX={agent_qos_rx} | Customer QoS: TX={customer_qos_tx} RX={customer_qos_rx}
Emotion: {emotion.get('customer_emotion_start')}→{emotion.get('customer_emotion_end')} | Frustration:{emotion.get('customer_frustration_level',0)} | Satisfaction:{emotion.get('customer_satisfaction_level',0)}
Agent tone:{emotion.get('agent_tone')} | Talk ratio:{emotion.get('agent_talk_ratio',50)}% | Uncertainty phrases:{uncertainty_phrases}
Audio:{audio_quality.get('overall')} | Noise:{audio_quality.get('background_noise',False)} | Explicit:{explicit_detected}

SPECIFIC TIMESTAMPED MOMENTS FOUND IN AUDIO (use these exact timestamps for any matching flag — do not invent your own):
{flagged_moments_text if flagged_moments_text else 'None detected by audio analysis.'}

QA RULES (evaluate each):
{rules_text}

EVIDENCE REQUIREMENT — APPLIES TO EVERY SINGLE RULE: the "evidence" field must always name the SPECIFIC thing that happened in THIS call, never a generic restatement of the rule itself. This applies whether the rule passed or failed.

NEVER acceptable: "Compliance violation", "No audio to verify", "Cannot confirm without transcript", "Passed", "Restricted content violated", "Age concern", "Dead air detected", "Professionalism issue".

ALWAYS required: name the exact moment, words, or behavior. Examples by rule type:
- Frustration rule: "Customer said 'I already told you this twice' at 3:10; agent moved on without acknowledging it" (fail) or "Customer raised voice about late delivery; agent said 'I understand how frustrating that must be' before continuing" (pass)
- Dead air rule: "Agent went silent for 52 seconds at 4:15 while searching account with no update given" (fail, always state the actual silence duration) or "Agent said 'still looking, one moment' every ~15s during a 40s search" (pass)
- Restricted content/age rule: name the EXACT trigger — "Customer stated she is 16 at 1:42, then asked agent to set up a dating app account" not "age concern"
- Professionalism rule: "Agent interrupted customer mid-sentence 3 times between 2:00-2:30" or "Agent said 'whatever, I guess' when customer asked for a refund" (fail) — never just "unprofessional"
- Billing rule: "Call lasted 22 min but only 15 min were billed — 7 min discrepancy" (cite the actual numbers)
- Active listening / instruction following: "Customer said the item was already saved on the account at 0:45; agent asked customer to repeat the item name from scratch at 1:10"
- If transcript is genuinely unusable: still be specific about what's missing — "Transcript empty due to audio failure; identity verification step, if performed, was not captured"

If you cannot find a specific moment for a PASSED rule, briefly state why it doesn't apply rather than inventing detail — e.g. "No competitor mentioned during this call" is fine and specific, even though short.

CATEGORIES: accuracy_and_information, customer_service_quality, active_listening, appropriate_actions, compliance_and_handling, emotion_management, script_and_language, documentation_quality, audio_and_technical, call_closure

NOTES SCORING: 0=no notes, 1-40=vague, 41-70=basic, 71-85=good, 86-100=excellent. Check notes match call.
CALL END: agent ended early + unresolved = flag. Drop + no callback = Critical.
AGE CONCERN: young-sounding customer + adult requests = human review.
RESTRICTED CONTENT / AGE RULE EVIDENCE: this rule covers multiple distinct triggers (caller under 18, caller appears under 18, minor requesting a smartphone, minor requesting adult/sexual/restricted products). When this rule fails, the evidence MUST name exactly which trigger occurred and what was said — e.g. "Customer stated she is 16 at 1:42, then asked agent to set up a dating app account" or "Customer's voice and word choice strongly suggest a child; asked agent to buy a smartphone without a parent present." Never write a generic phrase like "restricted content violated" or "age concern" — always specify which exact condition applied and the concrete words/context that triggered it.
OUTSIDE WORK: agent hinting at private work = Critical flag.
IGNORED INSTRUCTIONS: if customer states something was already provided/saved/discussed and agent proceeds as if starting fresh, flag under active_listening — this is distinct from simply not understanding something, it's specifically disregarding what the customer already told them.
UNANSWERED QUESTIONS: if customer asks a direct question and the agent's response never actually answers it (changes subject, answers something else, ignores it entirely), flag under customer_service_quality.
WRONG REGION/CURRENCY: if agent states a currency/region/country that contradicts the customer's stated context, flag under accuracy_and_information.
FLAG TIMESTAMPS: when a flag corresponds to one of the specific timestamped moments above, copy its exact timestamp_seconds value into that flag's "timestamp_seconds" field and its quote into "evidence_quote", so the user can jump straight to that second in the recording. If a flag is a general pattern across the whole call with no single moment (e.g. "agent talked too fast overall"), leave timestamp_seconds as 0.

Respond ONLY with valid compact JSON (evidence fields can be up to 140 chars to fit specific detail — see EVIDENCE REQUIREMENT above):

{{"overall_score":0,"confidence":0,"status":"Passed/Review/Critical","requires_human_review":false,"human_review_reason":"","category_scores":{{"accuracy_and_information":{{"score":0,"evidence":"brief","passed":true}},"customer_service_quality":{{"score":0,"evidence":"brief","passed":true}},"active_listening":{{"score":0,"evidence":"brief","passed":true}},"appropriate_actions":{{"score":0,"evidence":"brief","passed":true}},"compliance_and_handling":{{"score":0,"evidence":"brief","passed":true}},"emotion_management":{{"score":0,"evidence":"brief","passed":true}},"script_and_language":{{"score":0,"evidence":"brief","passed":true}},"documentation_quality":{{"score":0,"evidence":"brief","passed":true}},"audio_and_technical":{{"score":0,"evidence":"brief","passed":true}},"call_closure":{{"score":0,"evidence":"brief","passed":true}}}},"notes_score":0,"notes_feedback":"brief","rules_evaluation":[{{"rule":"rule text","category":"cat","severity":"Critical/Warning/Info","passed":true,"confidence":0,"evidence":"SPECIFIC detail from this exact call, max 100 chars"}}],"flags":[{{"title":"title","description":"under 100 chars","severity":"Critical/Warning","timestamp_seconds":0,"timestamp_display":"mm:ss","evidence_quote":"exact words if applicable, max 80 chars"}}],"emotion_delta":{{"start":"{emotion.get('customer_emotion_start','Neutral')}","end":"{emotion.get('customer_emotion_end','Neutral')}","improved":false,"summary":"brief"}},"age_concern":{{"flagged":false,"reason":""}},"call_end_assessment":"brief","coaching_notes":"2-3 points max","positive_highlights":"1-2 points max"}}"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = message.content[0].text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
            else:
                raise ValueError("No JSON found")
        except Exception:
            print(f"[Claude] Warning: Could not parse response, using fallback")
            result = {
                "overall_score": 0, "confidence": 0, "status": "Review",
                "requires_human_review": True,
                "human_review_reason": "AI scoring failed — manual review required",
                "category_scores": {}, "notes_score": 0, "notes_feedback": "",
                "rules_evaluation": [], "flags": [], "emotion_delta": {},
                "age_concern": {"flagged": False, "reason": ""},
                "call_end_assessment": "", "coaching_notes": "Manual review needed",
                "positive_highlights": ""
            }
    print(f"[Claude] Score: {result['overall_score']}% | Confidence: {result.get('confidence',0)}% | Notes: {result.get('notes_score',0)}%")

    # Track usage
    try:
        input_tok = message.usage.input_tokens
        output_tok = message.usage.output_tokens
        claude_cost = (input_tok / 1_000_000 * CLAUDE_INPUT_COST_PER_M) + (output_tok / 1_000_000 * CLAUDE_OUTPUT_COST_PER_M)
        track_usage('claude', None, input_tok, output_tok, 0, round(claude_cost, 6))
    except Exception as e:
        print(f"[Claude] Usage tracking skipped: {e}")

    return result

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def analyze_call(audio_path, agent_name, call_id=None, call_end_first='customer',
                 call_notes='', account_name='', agent_qos_tx='Good', agent_qos_rx='Good',
                 customer_qos_tx='Good', customer_qos_rx='Good', call_dropped=False):

    print(f"\n{'='*50}")
    print(f"[VoiceGuard] Agent: {agent_name} | Call: {call_id}")
    print(f"{'='*50}")

    # Step 1: Gemini
    gemini_result = analyze_audio_with_gemini(audio_path)

    # Step 2: Rules
    rules = load_active_rules()
    print(f"[VoiceGuard] {len(rules)} rules loaded")

    # Step 3: Claude scoring
    claude_result = score_call_with_claude(
        gemini_result, rules,
        call_end_first=call_end_first,
        call_notes=call_notes,
        account_name=account_name,
        agent_qos_tx=agent_qos_tx, agent_qos_rx=agent_qos_rx,
        customer_qos_tx=customer_qos_tx, customer_qos_rx=customer_qos_rx
    )

    # Step 4: Redact credentials
    safe_transcript = redact_credentials(gemini_result.get('transcript', ''))

    # Step 5: Emotion label
    emotion = gemini_result['emotion_analysis']
    emotion_overall = emotion.get('customer_emotion_overall', 'Neutral')
    emojis = {'Happy':'😊','Satisfied':'😊','Neutral':'😐','Frustrated':'😤','Angry':'😠','Confused':'😕'}
    emotion_label = f"{emojis.get(emotion_overall,'😐')} {emotion_overall}"

    # Step 6: Human review check
    confidence = claude_result.get('confidence', 100)
    requires_human_review = claude_result.get('requires_human_review', False)
    if confidence < 70:
        requires_human_review = True
        if not claude_result.get('human_review_reason'):
            claude_result['human_review_reason'] = f'Low AI confidence ({confidence}%) — needs human verification'

    audio_dropped = gemini_result.get('call_dropped_audio', False)
    final_call_dropped = call_dropped or audio_dropped or (call_end_first == 'drop')

    return {
        'call_id': call_id or f"CALL-{int(time.time())}",
        'agent_name': agent_name,
        'duration': gemini_result.get('duration_estimate', '--'),
        'overall_score': claude_result['overall_score'],
        'confidence': confidence,
        'status': claude_result['status'],
        'emotion': emotion_label,
        'emotion_delta': claude_result.get('emotion_delta', {}),
        'flags': len(claude_result.get('flags', [])),
        'transcript': safe_transcript,
        'summary': gemini_result.get('summary', ''),
        'scorecard': claude_result,
        'emotion_analysis': emotion,
        'audio_quality': gemini_result.get('audio_quality', {}),
        'requires_human_review': requires_human_review,
        'human_review_reason': claude_result.get('human_review_reason', ''),
        'call_dropped': final_call_dropped,
        'age_concern': claude_result.get('age_concern', {}),
        'coaching_notes': claude_result.get('coaching_notes', ''),
        'positive_highlights': claude_result.get('positive_highlights', ''),
        'notes_score': claude_result.get('notes_score', 0),
        'notes_feedback': claude_result.get('notes_feedback', ''),
        'call_end_assessment': claude_result.get('call_end_assessment', ''),
        'flagged_moments': gemini_result.get('flagged_moments', []),
        'is_callback': False,
        'original_call_id': None,
    }

# ─── GEMINI-ONLY PIPELINE (for side-by-side cost/quality comparison) ──────────
# Listens to audio AND scores against rules in a SINGLE Gemini call, instead of
# Gemini (listen) + Claude (score) as two separate calls. Used to evaluate whether
# Gemini alone can match Claude's scoring quality before fully switching over.

def analyze_and_score_with_gemini_only(audio_path, rules, call_end_first='customer',
                                         call_notes='', account_name='', agent_qos_tx='Good',
                                         agent_qos_rx='Good', customer_qos_tx='Good', customer_qos_rx='Good'):
    """
    Single-call Gemini pipeline: listens to the audio AND evaluates it against
    the QA rules in one request, producing the same JSON shape as Claude's
    score_call_with_claude() so results are directly comparable.
    """
    print(f"[Gemini-Only] Analyzing + scoring: {audio_path}")
    model = genai.GenerativeModel('gemini-2.5-flash')

    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {'.mp3':'audio/mp3','.wav':'audio/wav','.m4a':'audio/mp4',
                '.ogg':'audio/ogg','.flac':'audio/flac','.webm':'audio/webm'}
    mime_type = mime_map.get(ext, 'audio/wav')

    INLINE_SIZE_LIMIT_MB = 18
    audio_size_mb = os.path.getsize(audio_path) / 1024 / 1024

    if audio_size_mb <= INLINE_SIZE_LIMIT_MB:
        with open(audio_path, 'rb') as f:
            audio_data = f.read()
        audio_b64 = base64.b64encode(audio_data).decode('utf-8')
        audio_part = {'mime_type': mime_type, 'data': audio_b64}
        uploaded_file_name = None
    else:
        api_key = os.getenv('GEMINI_API_KEY')
        file_size = os.path.getsize(audio_path)
        start_resp = requests.post(
            f'https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}',
            headers={
                'X-Goog-Upload-Protocol': 'resumable', 'X-Goog-Upload-Command': 'start',
                'X-Goog-Upload-Header-Content-Length': str(file_size),
                'X-Goog-Upload-Header-Content-Type': mime_type, 'Content-Type': 'application/json',
            },
            json={'file': {'display_name': os.path.basename(audio_path)}}, timeout=30
        )
        start_resp.raise_for_status()
        upload_url = start_resp.headers.get('X-Goog-Upload-URL')
        with open(audio_path, 'rb') as f:
            file_data = f.read()
        upload_resp = requests.post(upload_url, headers={
            'X-Goog-Upload-Offset': '0', 'X-Goog-Upload-Command': 'upload, finalize',
            'Content-Length': str(file_size),
        }, data=file_data, timeout=120)
        upload_resp.raise_for_status()
        file_info = upload_resp.json().get('file', {})
        file_uri = file_info.get('uri')
        file_name = file_info.get('name')
        file_state = file_info.get('state', 'PROCESSING')
        for _ in range(15):
            if file_state == 'ACTIVE': break
            check_resp = requests.get(f'https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}', timeout=15)
            file_state = check_resp.json().get('state', 'PROCESSING')
            if file_state == 'ACTIVE': break
            time.sleep(2)
        audio_part = {'file_data': {'mime_type': mime_type, 'file_uri': file_uri}}
        uploaded_file_name = file_name

    rules_text = '\n'.join([f"- [{r['severity'].upper()}] ({r['category']}) {r['description']}" for r in rules])
    call_end_context = {
        'agent': 'AGENT ended the call first — evaluate if call was fully resolved before hangup',
        'customer': 'Customer ended the call normally',
        'drop': 'Call DROPPED unexpectedly — evaluate if agent attempted callback'
    }.get(call_end_first, 'Unknown')
    notes_context = f'Agent call notes: "{call_notes}"' if call_notes else 'Agent wrote NO call notes after this call'
    customer_context = f'Customer: {account_name}' if account_name else ''

    prompt = f"""You are a call center QA analyst. Listen to this call recording carefully and score it strictly and concisely against the rules below — you must BOTH transcribe/understand the audio AND evaluate compliance in this single pass.

METADATA:
{customer_context} | Call end: {call_end_context} | {notes_context}
Agent QoS: TX={agent_qos_tx} RX={agent_qos_rx} | Customer QoS: TX={customer_qos_tx} RX={customer_qos_rx}

QA RULES (evaluate each against what you hear in the audio):
{rules_text}

EVIDENCE REQUIREMENT — APPLIES TO EVERY SINGLE RULE: the "evidence" field must always name the SPECIFIC thing that happened in THIS call — a real quote, timestamp, or concrete observation — never a generic restatement of the rule. This applies whether the rule passed or failed.

NEVER acceptable: "Compliance violation", "No audio to verify", "Passed", "Restricted content violated", "Age concern", "Dead air detected", "Professionalism issue".

ALWAYS required: name the exact moment, words, or behavior, e.g. "Customer said 'I already told you this twice' at 3:10; agent moved on without acknowledging it" or "Agent went silent for 52 seconds at 4:15 while searching with no update given."

CATEGORIES: accuracy_and_information, customer_service_quality, active_listening, appropriate_actions, compliance_and_handling, emotion_management, script_and_language, documentation_quality, audio_and_technical, call_closure

NOTES SCORING: 0=no notes, 1-40=vague, 41-70=basic, 71-85=good, 86-100=excellent. Check notes match call content.
CALL END: agent ended early + unresolved = flag. Drop + no callback = Critical.
AGE CONCERN: young-sounding customer + adult requests = human review.
OUTSIDE WORK: agent hinting at private work = Critical flag.
IGNORED INSTRUCTIONS: if customer states something was already provided/saved/discussed and agent proceeds as if starting fresh, flag under active_listening.
UNANSWERED QUESTIONS: if customer asks a direct question and agent's response never answers it, flag under customer_service_quality.
WRONG REGION/CURRENCY: if agent states a currency/region/country contradicting the customer's context, flag under accuracy_and_information.
FLAG TIMESTAMPS: for any flag tied to a specific moment you hear, include the real timestamp_seconds and evidence_quote. If a flag is a general whole-call pattern, leave timestamp_seconds as 0.
AUDIO QUALITY: assess background noise, audio cuts/drops, and overall clarity directly from what you hear.
EMOTION: track customer's emotional arc from start to end of the call directly from tone of voice.

Respond ONLY with valid compact JSON (evidence fields up to 140 chars):

{{"transcript":"Summarized transcript max 2500 chars, label AGENT: and CUSTOMER:","duration_estimate":"mm:ss","summary":"2-3 sentence summary","overall_score":0,"confidence":0,"status":"Passed/Review/Critical","requires_human_review":false,"human_review_reason":"","emotion_start":"Happy/Satisfied/Neutral/Frustrated/Angry/Confused","emotion_end":"Happy/Satisfied/Neutral/Frustrated/Angry/Confused","audio_quality_overall":"Good/Fair/Poor","background_noise":false,"category_scores":{{"accuracy_and_information":{{"score":0,"evidence":"brief","passed":true}},"customer_service_quality":{{"score":0,"evidence":"brief","passed":true}},"active_listening":{{"score":0,"evidence":"brief","passed":true}},"appropriate_actions":{{"score":0,"evidence":"brief","passed":true}},"compliance_and_handling":{{"score":0,"evidence":"brief","passed":true}},"emotion_management":{{"score":0,"evidence":"brief","passed":true}},"script_and_language":{{"score":0,"evidence":"brief","passed":true}},"documentation_quality":{{"score":0,"evidence":"brief","passed":true}},"audio_and_technical":{{"score":0,"evidence":"brief","passed":true}},"call_closure":{{"score":0,"evidence":"brief","passed":true}}}},"notes_score":0,"notes_feedback":"brief","rules_evaluation":[{{"rule":"rule text","category":"cat","severity":"Critical/Warning/Info","passed":true,"confidence":0,"evidence":"SPECIFIC detail, max 140 chars"}}],"flags":[{{"title":"title","description":"under 100 chars","severity":"Critical/Warning","timestamp_seconds":0,"timestamp_display":"mm:ss","evidence_quote":"exact words if applicable, max 80 chars"}}],"emotion_delta":{{"start":"Neutral","end":"Neutral","improved":false,"summary":"brief"}},"age_concern":{{"flagged":false,"reason":""}},"call_end_assessment":"brief","coaching_notes":"2-3 points max","positive_highlights":"1-2 points max"}}"""

    response = model.generate_content(
        [audio_part, prompt],
        generation_config=genai.GenerationConfig(max_output_tokens=8192, temperature=0.1)
    )

    if uploaded_file_name:
        try:
            api_key = os.getenv('GEMINI_API_KEY')
            requests.delete(f'https://generativelanguage.googleapis.com/v1beta/{uploaded_file_name}?key={api_key}', timeout=15)
        except Exception as e:
            print(f"[Gemini-Only] Could not delete uploaded file: {e}")

    # Robustly extract text — response.text throws if Gemini returned no valid candidate
    # (blocked content, safety filter, or empty response). Capture the real reason.
    text = ''
    try:
        text = response.text.strip()
    except Exception as e:
        finish_reason = 'unknown'
        try:
            finish_reason = str(response.candidates[0].finish_reason) if response.candidates else 'no candidates'
        except Exception:
            pass
        raise Exception(f"Gemini returned no usable text (finish_reason={finish_reason}): {str(e)[:200]}")

    if not text:
        raise Exception("Gemini returned an empty response for the combined listen+score request")

    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[Gemini-Only] JSON parse failed: {e}")
        result = {
            "transcript": "Analysis failed — could not parse Gemini response.",
            "overall_score": 0, "confidence": 0, "status": "Review",
            "requires_human_review": True, "human_review_reason": "Gemini-only analysis failed to parse.",
            "category_scores": {}, "notes_score": 0, "notes_feedback": "",
            "rules_evaluation": [], "flags": [], "emotion_delta": {}, "age_concern": {},
            "call_end_assessment": "", "coaching_notes": "", "positive_highlights": "",
            "summary": "", "duration_estimate": "--", "emotion_start": "Neutral",
            "emotion_end": "Neutral", "audio_quality_overall": "Unknown", "background_noise": False
        }

    # Track usage (input/output tokens) the same way as the two-call pipeline, so
    # cost comparisons in the dashboard are accurate
    try:
        estimated_mins = max(1, round(audio_size_mb))
        output_tokens = len(text) // 4  # rough token estimate
        try:
            usage = response.usage_metadata
            input_tokens = usage.prompt_token_count
            output_tokens = usage.candidates_token_count
        except Exception:
            input_tokens = 0  # fall back to rough estimate above if SDK doesn't expose usage_metadata
        gemini_only_cost = (estimated_mins * GEMINI_AUDIO_COST_PER_MIN) + (output_tokens / 1_000_000 * GEMINI_OUTPUT_COST_PER_M)
        track_usage('gemini-only', None, input_tokens, output_tokens, estimated_mins * 60, round(gemini_only_cost, 6))
    except Exception as e:
        print(f"[Gemini-Only] Could not track usage: {e}")

    emotion_label_map = {'Happy':'😊','Satisfied':'😊','Neutral':'😐','Frustrated':'😤','Angry':'😠','Confused':'😕'}
    emotion_overall = result.get('emotion_end', 'Neutral')
    emotion_label = f"{emotion_label_map.get(emotion_overall,'😐')} {emotion_overall}"

    confidence = result.get('confidence', 100)
    requires_human_review = result.get('requires_human_review', False)
    if confidence < 70:
        requires_human_review = True
        if not result.get('human_review_reason'):
            result['human_review_reason'] = f'Low AI confidence ({confidence}%) — needs human verification'

    return {
        'duration': result.get('duration_estimate', '--'),
        'overall_score': result.get('overall_score', 0),
        'confidence': confidence,
        'status': result.get('status', 'Review'),
        'emotion': emotion_label,
        'emotion_delta': result.get('emotion_delta', {}),
        'flags': len(result.get('flags', [])),
        'transcript': result.get('transcript', ''),
        'summary': result.get('summary', ''),
        'scorecard': result,
        'audio_quality': {'overall': result.get('audio_quality_overall', 'Unknown'), 'background_noise': result.get('background_noise', False)},
        'requires_human_review': requires_human_review,
        'human_review_reason': result.get('human_review_reason', ''),
        'age_concern': result.get('age_concern', {}),
        'coaching_notes': result.get('coaching_notes', ''),
        'positive_highlights': result.get('positive_highlights', ''),
        'notes_score': result.get('notes_score', 0),
        'notes_feedback': result.get('notes_feedback', ''),
        'call_end_assessment': result.get('call_end_assessment', ''),
        'flagged_moments': result.get('flags', []),  # reuse flags as moments for this simplified pipeline
        'pipeline': 'gemini-only',
    }


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        result = analyze_call(sys.argv[1], sys.argv[2],
                             call_id=sys.argv[3] if len(sys.argv) > 3 else None)
        print(f"\n✅ Score: {result['overall_score']}%")
        print(f"   Notes Score: {result['notes_score']}%")
        print(f"   Confidence: {result['confidence']}%")
        print(f"   Status: {result['status']}")
        print(f"   Emotion: {result['emotion']}")
        print(f"   Human Review: {result['requires_human_review']}")
        print(f"   Flags: {result['flags']}")
    else:
        print("Usage: python ai_engine.py <audio_file> <agent_name> [call_id]")