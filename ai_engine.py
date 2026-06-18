import os
import json
import re
import time
import base64
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
        # Large file — upload via Gemini File API first, then reference it
        print(f"[Gemini] File too large for inline ({round(audio_size_mb,1)}MB > {INLINE_SIZE_LIMIT_MB}MB), uploading via File API...")
        uploaded = genai.upload_file(audio_path, mime_type=mime_type)
        # Wait for file to become ACTIVE (usually instant for audio)
        import time as _time
        for _ in range(10):
            file_status = genai.get_file(uploaded.name)
            if file_status.state.name == 'ACTIVE':
                break
            _time.sleep(2)
        audio_part = uploaded  # Gemini accepts the file object directly
        print(f"[Gemini] File uploaded: {uploaded.name}")

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
            genai.delete_file(audio_part.name)
            print(f"[Gemini] Cleaned up uploaded file: {audio_part.name}")
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

CATEGORIES: accuracy_and_information, customer_service_quality, active_listening, appropriate_actions, compliance_and_handling, emotion_management, script_and_language, documentation_quality, audio_and_technical, call_closure

NOTES SCORING: 0=no notes, 1-40=vague, 41-70=basic, 71-85=good, 86-100=excellent. Check notes match call.
CALL END: agent ended early + unresolved = flag. Drop + no callback = Critical.
AGE CONCERN: young-sounding customer + adult requests = human review.
OUTSIDE WORK: agent hinting at private work = Critical flag.
IGNORED INSTRUCTIONS: if customer states something was already provided/saved/discussed and agent proceeds as if starting fresh, flag under active_listening — this is distinct from simply not understanding something, it's specifically disregarding what the customer already told them.
UNANSWERED QUESTIONS: if customer asks a direct question and the agent's response never actually answers it (changes subject, answers something else, ignores it entirely), flag under customer_service_quality.
WRONG REGION/CURRENCY: if agent states a currency/region/country that contradicts the customer's stated context, flag under accuracy_and_information.
FLAG TIMESTAMPS: when a flag corresponds to one of the specific timestamped moments above, copy its exact timestamp_seconds value into that flag's "timestamp_seconds" field and its quote into "evidence_quote", so the user can jump straight to that second in the recording. If a flag is a general pattern across the whole call with no single moment (e.g. "agent talked too fast overall"), leave timestamp_seconds as 0.

Respond ONLY with valid compact JSON (keep evidence under 60 chars each):

{{"overall_score":0,"confidence":0,"status":"Passed/Review/Critical","requires_human_review":false,"human_review_reason":"","category_scores":{{"accuracy_and_information":{{"score":0,"evidence":"brief","passed":true}},"customer_service_quality":{{"score":0,"evidence":"brief","passed":true}},"active_listening":{{"score":0,"evidence":"brief","passed":true}},"appropriate_actions":{{"score":0,"evidence":"brief","passed":true}},"compliance_and_handling":{{"score":0,"evidence":"brief","passed":true}},"emotion_management":{{"score":0,"evidence":"brief","passed":true}},"script_and_language":{{"score":0,"evidence":"brief","passed":true}},"documentation_quality":{{"score":0,"evidence":"brief","passed":true}},"audio_and_technical":{{"score":0,"evidence":"brief","passed":true}},"call_closure":{{"score":0,"evidence":"brief","passed":true}}}},"notes_score":0,"notes_feedback":"brief","rules_evaluation":[{{"rule":"rule text","category":"cat","severity":"Critical/Warning/Info","passed":true,"confidence":0,"evidence":"brief"}}],"flags":[{{"title":"title","description":"under 100 chars","severity":"Critical/Warning","timestamp_seconds":0,"timestamp_display":"mm:ss","evidence_quote":"exact words if applicable, max 80 chars"}}],"emotion_delta":{{"start":"{emotion.get('customer_emotion_start','Neutral')}","end":"{emotion.get('customer_emotion_end','Neutral')}","improved":false,"summary":"brief"}},"age_concern":{{"flagged":false,"reason":""}},"call_end_assessment":"brief","coaching_notes":"2-3 points max","positive_highlights":"1-2 points max"}}"""

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
