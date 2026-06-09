import os
import json
import re
import sqlite3
import base64
import time
import anthropic
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ─── SETUP ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

DATABASE_URL = os.getenv('DATABASE_URL', '')

def get_db():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        import sqlite3
        conn = sqlite3.connect(os.path.join(os.getenv('HOME', '.'), 'voiceguard.db'))
        return conn

# ─── CREDENTIAL REDACTION ─────────────────────────────────────────────────────
def redact_credentials(text):
    """Remove sensitive data from transcript before saving."""
    if not text:
        return text

    # Redact passwords (common patterns)
    text = re.sub(r'(?i)(password[:\s]+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(my password is[:\s]+)\S+', r'\1[REDACTED]', text)

    # Redact OTP/verification codes (4-8 digit codes)
    text = re.sub(r'(?i)(code[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(verification[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(otp[:\s]+)\d{4,8}', r'\1[REDACTED]', text)
    text = re.sub(r'\b\d{6}\b', '[OTP-REDACTED]', text)

    # Redact credit card numbers
    text = re.sub(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', '[CARD-REDACTED]', text)

    # Redact SSN
    text = re.sub(r'\b\d{3}[-]\d{2}[-]\d{4}\b', '[SSN-REDACTED]', text)

    return text

# ─── LOAD ACTIVE RULES ────────────────────────────────────────────────────────
def load_active_rules():
    try:
        conn = get_db()
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            c = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute("SELECT description, category, severity FROM rules WHERE active=1 ORDER BY severity DESC")
        rules = [dict(r) for r in c.fetchall()]
        conn.close()
        return rules
    except Exception as e:
        print(f"[Warning] Could not load rules from DB: {e}")
        return []

# ─── STEP 1: GEMINI AUDIO ANALYSIS ───────────────────────────────────────────
def analyze_audio_with_gemini(audio_path):
    print(f"[Gemini] Analyzing audio: {audio_path}")

    model = genai.GenerativeModel('gemini-2.5-flash')

    with open(audio_path, 'rb') as f:
        audio_data = f.read()

    audio_b64 = base64.b64encode(audio_data).decode('utf-8')

    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {
        '.mp3': 'audio/mp3', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
        '.ogg': 'audio/ogg', '.flac': 'audio/flac', '.webm': 'audio/webm',
    }
    mime_type = mime_map.get(ext, 'audio/wav')

    prompt = """You are an expert call center audio analyst. Listen carefully to this customer service call.

Analyze EVERYTHING — words spoken, tone, emotion, pace, silences, background noise, and audio quality.

Respond ONLY with this exact JSON format, no other text:

{
  "transcript": "Full word-for-word transcript. Label each line as AGENT: or CUSTOMER:",
  "duration_estimate": "e.g. 5:32",
  "audio_quality": {
    "overall": "Good/Fair/Poor",
    "background_noise": true or false,
    "background_noise_description": "describe if present",
    "audio_cuts_or_drops": true or false
  },
  "emotion_analysis": {
    "customer_emotion_start": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_end": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_overall": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_frustration_level": 0-100,
    "customer_satisfaction_level": 0-100,
    "customer_repeated_themselves": true or false,
    "customer_repeated_count": 0,
    "customer_sounds_young": true or false,
    "customer_raised_voice": true or false,
    "agent_tone": "Professional/Friendly/Neutral/Rushed/Defensive/Robotic/Rude",
    "agent_stress_level": 0-100,
    "agent_speaking_speed": "Too Fast/Normal/Too Slow",
    "agent_talk_ratio": 0-100,
    "agent_interrupted_customer": true or false,
    "long_silences_detected": true or false,
    "longest_silence_seconds": 0,
    "key_emotional_moments": [
      {
        "timestamp": "e.g. 2:14",
        "description": "what happened emotionally"
      }
    ]
  },
  "call_dropped": true or false,
  "summary": "2-3 sentence summary of what the call was about and how it went",
  "agent_uncertainty_phrases": ["list any phrases like I am not sure, I think so, maybe, etc detected"],
  "explicit_content_detected": true or false,
  "explicit_content_description": "describe if detected, empty string otherwise"
}"""

    response = model.generate_content([
        {'mime_type': mime_type, 'data': audio_b64},
        prompt
    ])

    text = response.text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    result = json.loads(text)
    print(f"[Gemini] Done. Customer emotion: {result['emotion_analysis']['customer_emotion_overall']}")
    return result

# ─── STEP 2: CLAUDE QA SCORING ────────────────────────────────────────────────
def score_call_with_claude(gemini_result, rules):
    print(f"[Claude] Scoring against {len(rules)} rules...")

    transcript = gemini_result.get('transcript', '')
    emotion = gemini_result.get('emotion_analysis', {})
    audio_quality = gemini_result.get('audio_quality', {})
    summary = gemini_result.get('summary', '')
    uncertainty_phrases = gemini_result.get('agent_uncertainty_phrases', [])
    explicit_detected = gemini_result.get('explicit_content_detected', False)

    rules_text = '\n'.join([
        f"- [{r['severity'].upper()}] ({r['category']}) {r['description']}"
        for r in rules
    ])

    prompt = f"""You are an expert call center QA analyst. Evaluate this customer service call with precision.

═══ TRANSCRIPT ═══
{transcript}

═══ AUDIO ANALYSIS FROM GEMINI ═══
Customer emotion at START: {emotion.get('customer_emotion_start', 'Unknown')}
Customer emotion at END: {emotion.get('customer_emotion_end', 'Unknown')}
Customer frustration level: {emotion.get('customer_frustration_level', 0)}/100
Customer satisfaction level: {emotion.get('customer_satisfaction_level', 0)}/100
Customer repeated themselves: {emotion.get('customer_repeated_themselves', False)} ({emotion.get('customer_repeated_count', 0)} times)
Customer raised voice: {emotion.get('customer_raised_voice', False)}
Customer sounds young (possible minor): {emotion.get('customer_sounds_young', False)}
Agent tone: {emotion.get('agent_tone', 'Unknown')}
Agent stress level: {emotion.get('agent_stress_level', 0)}/100
Agent speaking speed: {emotion.get('agent_speaking_speed', 'Normal')}
Agent talk ratio: {emotion.get('agent_talk_ratio', 50)}% (agent spoke this % of the call)
Agent interrupted customer: {emotion.get('agent_interrupted_customer', False)}
Long silences detected: {emotion.get('long_silences_detected', False)}
Longest silence: {emotion.get('longest_silence_seconds', 0)} seconds
Agent uncertainty phrases detected: {uncertainty_phrases}
Audio quality: {audio_quality.get('overall', 'Unknown')}
Background noise: {audio_quality.get('background_noise', False)} - {audio_quality.get('background_noise_description', '')}
Explicit content detected by audio AI: {explicit_detected}
Key emotional moments: {json.dumps(emotion.get('key_emotional_moments', []))}
Call summary: {summary}

═══ ACTIVE QA RULES (evaluate every single one) ═══
{rules_text}

═══ STANDARD QA CATEGORIES ═══
1. Accuracy & Information — correct info, followed policies
2. Customer Service Quality — professional, empathetic, clear, courteous
3. Active Listening — did not interrupt, let customer finish, acknowledged concerns
4. Appropriate Actions — correct actions, followed workflows, documented properly
5. Compliance & Call Handling — verified identity, followed compliance requirements
6. Emotion & Tone Management — how well agent handled customer emotions, improved mood
7. Script & Language Quality — no uncertainty phrases, professional language, no prohibited phrases
8. Audio & Technical Quality — background noise, call drops, audio issues

═══ SPECIAL CHECKS ═══
- If customer sounds young AND requests phones/age-restricted products: flag for age verification
- If agent discusses working outside Proclick or solicits personal contact: Critical flag
- If agent mentions wrong region/currency vs customer context: flag
- If agent fails to acknowledge repeated customer complaints: flag
- Any cultural, explicit, or prohibited content violations: Critical flag

Respond ONLY with this exact JSON:

{{
  "overall_score": 0-100,
  "confidence": 0-100,
  "status": "Passed or Review or Critical",
  "requires_human_review": true or false,
  "human_review_reason": "reason if requires_human_review is true, else empty",
  "category_scores": {{
    "accuracy_and_information": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "customer_service_quality": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "active_listening": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "appropriate_actions": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "compliance_and_handling": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "emotion_management": {{"score": 0-100, "evidence": "emotion delta: start={emotion.get('customer_emotion_start')} end={emotion.get('customer_emotion_end')}", "passed": true/false}},
    "script_and_language": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}},
    "audio_and_technical": {{"score": 0-100, "evidence": "specific evidence", "passed": true/false}}
  }},
  "rules_evaluation": [
    {{
      "rule": "exact rule text",
      "category": "rule category",
      "severity": "Critical or Warning or Info",
      "passed": true or false,
      "confidence": 0-100,
      "evidence": "specific quote or observation"
    }}
  ],
  "flags": [
    {{
      "severity": "Critical or Warning",
      "type": "flag type e.g. forbidden_language, age_concern, billing_concern, script_deviation",
      "title": "short flag title",
      "description": "what happened and why it matters",
      "timestamp": "approximate time if known"
    }}
  ],
  "emotion_delta": {{
    "improved": true or false,
    "start": "{emotion.get('customer_emotion_start', 'Unknown')}",
    "end": "{emotion.get('customer_emotion_end', 'Unknown')}",
    "summary": "one sentence describing emotional arc of the call"
  }},
  "age_concern": {{
    "flagged": true or false,
    "reason": "reason if flagged, else empty"
  }},
  "billing_concern": {{
    "flagged": true or false,
    "reason": "reason if flagged, else empty"
  }},
  "coaching_notes": "2-3 specific coaching points for this agent",
  "positive_highlights": "1-2 things agent did well"
}}"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = message.content[0].text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    result = json.loads(text)
    print(f"[Claude] Score: {result['overall_score']}% | Confidence: {result.get('confidence', 0)}% | Status: {result['status']}")
    return result

# ─── CALLBACK DETECTION ───────────────────────────────────────────────────────
def check_callback(caller_id, call_id, call_time):
    """Check if this call is a callback after a dropped call."""
    if not caller_id or not DATABASE_URL:
        return False, None

    try:
        conn = get_db()
        from psycopg2.extras import RealDictCursor
        c = conn.cursor(cursor_factory=RealDictCursor)

        # Look for a dropped call from same caller_id in last 10 minutes
        c.execute('''
            SELECT call_id FROM calls
            WHERE caller_id = %s
            AND call_dropped = true
            AND call_id != %s
            AND created_at >= NOW() - INTERVAL '10 minutes'
            ORDER BY created_at DESC
            LIMIT 1
        ''', (caller_id, call_id))

        dropped_call = c.fetchone()
        conn.close()

        if dropped_call:
            return True, dropped_call['call_id']
        return False, None
    except Exception as e:
        print(f"[Warning] Callback check failed: {e}")
        return False, None

def mark_callback_completed(original_call_id, callback_call_id):
    """Mark the original dropped call as having received a callback."""
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE calls SET callback_call_id=%s, callback_made=true WHERE call_id=%s",
            (callback_call_id, original_call_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Warning] Could not mark callback: {e}")

# ─── MAIN ANALYZE FUNCTION ────────────────────────────────────────────────────
def analyze_call(audio_path, agent_name, call_id=None, caller_id=None, call_dropped=False):
    print(f"\n{'='*50}")
    print(f"[VoiceGuard] Analyzing: {agent_name} | Call: {call_id}")
    print(f"{'='*50}")

    # Step 1: Gemini audio analysis
    gemini_result = analyze_audio_with_gemini(audio_path)

    # Step 2: Load active rules
    rules = load_active_rules()
    print(f"[VoiceGuard] {len(rules)} active rules loaded")

    # Step 3: Claude QA scoring
    claude_result = score_call_with_claude(gemini_result, rules)

    # Step 4: Redact credentials from transcript
    raw_transcript = gemini_result.get('transcript', '')
    safe_transcript = redact_credentials(raw_transcript)

    # Step 5: Check for callback (was this a callback after a dropped call?)
    is_callback, original_call_id = False, None
    if caller_id:
        is_callback, original_call_id = check_callback(caller_id, call_id, time.time())
        if is_callback and original_call_id:
            mark_callback_completed(original_call_id, call_id)
            print(f"[VoiceGuard] Callback detected — linked to dropped call {original_call_id}")

    # Step 6: Build emotion label
    emotion = gemini_result['emotion_analysis']
    emotion_overall = emotion.get('customer_emotion_overall', 'Neutral')
    emotion_emojis = {
        'Happy': '😊', 'Satisfied': '😊', 'Neutral': '😐',
        'Frustrated': '😤', 'Angry': '😠', 'Confused': '😕'
    }
    emotion_label = f"{emotion_emojis.get(emotion_overall, '😐')} {emotion_overall}"

    # Step 7: Determine if human review needed
    confidence = claude_result.get('confidence', 100)
    requires_human_review = claude_result.get('requires_human_review', False)
    if confidence < 70:
        requires_human_review = True

    final_result = {
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
        'is_callback': is_callback,
        'original_call_id': original_call_id,
        'call_dropped': call_dropped or gemini_result.get('call_dropped', False),
        'age_concern': claude_result.get('age_concern', {}),
        'coaching_notes': claude_result.get('coaching_notes', ''),
        'positive_highlights': claude_result.get('positive_highlights', ''),
    }

    print(f"[VoiceGuard] ✅ Complete — Score: {final_result['overall_score']}% | Confidence: {confidence}%")
    return final_result

# ─── TEST MODE ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        audio_file = sys.argv[1]
        agent = sys.argv[2]
        call_id = sys.argv[3] if len(sys.argv) > 3 else None
        result = analyze_call(audio_file, agent, call_id)
        print(f"\n✅ Score: {result['overall_score']}%")
        print(f"   Status: {result['status']}")
        print(f"   Confidence: {result['confidence']}%")
        print(f"   Emotion: {result['emotion']}")
        print(f"   Emotion improved: {result['emotion_delta'].get('improved', 'N/A')}")
        print(f"   Flags: {result['flags']}")
        print(f"   Human review needed: {result['requires_human_review']}")
    else:
        print("Usage: python ai_engine.py <audio_file> <agent_name> [call_id]")
