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

    with open(audio_path, 'rb') as f:
        audio_data = f.read()
    audio_b64 = base64.b64encode(audio_data).decode('utf-8')

    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {'.mp3':'audio/mp3','.wav':'audio/wav','.m4a':'audio/mp4',
                '.ogg':'audio/ogg','.flac':'audio/flac','.webm':'audio/webm'}
    mime_type = mime_map.get(ext, 'audio/wav')

    prompt = """You are an expert call center audio analyst. Listen carefully to every detail.

Respond ONLY with this exact JSON — no other text:

{
  "transcript": "Full transcript. Label each line AGENT: or CUSTOMER:",
  "duration_estimate": "e.g. 5:32",
  "audio_quality": {
    "overall": "Good/Fair/Poor",
    "background_noise": true/false,
    "background_noise_description": "describe if present",
    "audio_cuts_or_drops": true/false
  },
  "emotion_analysis": {
    "customer_emotion_start": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_end": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_emotion_overall": "Happy/Satisfied/Neutral/Frustrated/Angry/Confused",
    "customer_frustration_level": 0-100,
    "customer_satisfaction_level": 0-100,
    "customer_repeated_themselves": true/false,
    "customer_repeated_count": 0,
    "customer_sounds_young": true/false,
    "customer_raised_voice": true/false,
    "agent_tone": "Professional/Friendly/Neutral/Rushed/Defensive/Robotic/Rude",
    "agent_stress_level": 0-100,
    "agent_speaking_speed": "Too Fast/Normal/Too Slow",
    "agent_talk_ratio": 0-100,
    "agent_interrupted_customer": true/false,
    "long_silences_detected": true/false,
    "longest_silence_seconds": 0,
    "key_emotional_moments": [{"timestamp": "2:14", "description": "what happened"}]
  },
  "call_dropped_audio": true/false,
  "summary": "2-3 sentence summary",
  "agent_uncertainty_phrases": ["list phrases like I am not sure, maybe, I think so"],
  "explicit_content_detected": true/false,
  "explicit_content_description": ""
}"""

    response = model.generate_content([{'mime_type': mime_type, 'data': audio_b64}, prompt])

    text = response.text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]

    result = json.loads(text.strip())
    print(f"[Gemini] Done. Emotion: {result['emotion_analysis']['customer_emotion_overall']}")
    return result

# ─── CLAUDE QA SCORING ────────────────────────────────────────────────────────
def score_call_with_claude(gemini_result, rules, call_end_first='customer',
                            call_notes='', account_name='', line_issues='none'):
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

    # Build call context
    call_end_context = {
        'agent': 'AGENT ended the call first — evaluate if call was fully resolved before hangup',
        'customer': 'Customer ended the call normally',
        'drop': 'Call DROPPED unexpectedly — evaluate if agent attempted callback'
    }.get(call_end_first, 'Unknown')

    line_issues_context = {
        'none': 'No line issues reported',
        'agent': 'Line issues on AGENT side — factor into audio quality score',
        'customer': 'Line issues on CUSTOMER side — less agent responsibility for audio quality'
    }.get(line_issues, 'Unknown')

    notes_context = f'Agent call notes: "{call_notes}"' if call_notes else 'Agent wrote NO call notes after this call'
    customer_context = f'Customer: {account_name}' if account_name else ''

    prompt = f"""You are an expert call center QA analyst. Evaluate every aspect of this call.

═══ TRANSCRIPT ═══
{transcript}

═══ CALL METADATA ═══
{customer_context}
Call ended by: {call_end_context}
Line issues: {line_issues_context}
{notes_context}

═══ AUDIO ANALYSIS ═══
Customer emotion START: {emotion.get('customer_emotion_start')}
Customer emotion END: {emotion.get('customer_emotion_end')}
Customer frustration: {emotion.get('customer_frustration_level',0)}/100
Customer satisfaction: {emotion.get('customer_satisfaction_level',0)}/100
Customer repeated themselves: {emotion.get('customer_repeated_themselves',False)} ({emotion.get('customer_repeated_count',0)} times)
Customer raised voice: {emotion.get('customer_raised_voice',False)}
Customer sounds young (possible minor): {emotion.get('customer_sounds_young',False)}
Agent tone: {emotion.get('agent_tone')}
Agent stress: {emotion.get('agent_stress_level',0)}/100
Agent speaking speed: {emotion.get('agent_speaking_speed')}
Agent talk ratio: {emotion.get('agent_talk_ratio',50)}% (higher = agent dominated)
Agent interrupted customer: {emotion.get('agent_interrupted_customer',False)}
Long silences (>30s): {emotion.get('long_silences_detected',False)} — longest: {emotion.get('longest_silence_seconds',0)}s
Agent uncertainty phrases: {uncertainty_phrases}
Audio quality: {audio_quality.get('overall')} | Background noise: {audio_quality.get('background_noise',False)}
Explicit content detected: {explicit_detected}
Key moments: {json.dumps(emotion.get('key_emotional_moments',[]))}

═══ ACTIVE QA RULES ═══
{rules_text}

═══ QA CATEGORIES ═══
1. Accuracy & Information — correct info, followed policies
2. Customer Service Quality — professional, empathetic, courteous  
3. Active Listening — did not interrupt, acknowledged concerns, customer did not repeat
4. Appropriate Actions — correct actions, workflows followed
5. Compliance & Handling — identity verified, price confirmed, logged out
6. Emotion Management — improved customer mood, handled frustration well
7. Script & Language — no uncertainty phrases, professional language
8. Documentation Quality — notes are detailed, accurate, match call content
9. Audio & Technical — background noise, call quality, line issues considered
10. Call Closure — proper goodbye, asked if anything else needed, appropriate who ended call

═══ SPECIAL EVALUATIONS ═══
NOTES EVALUATION: Score the call notes 0-100:
- 0 = No notes written (Critical issue)
- 1-40 = Vague notes ("helped customer") — Poor
- 41-70 = Basic notes with some detail — Fair  
- 71-85 = Good notes with account, action taken, outcome — Good
- 86-100 = Excellent — detailed, professional, matches call perfectly
Also check: do notes MATCH what actually happened? Flag mismatches.

CALL END EVALUATION:
- If agent ended first AND call was incomplete/customer still had questions → Warning/Critical
- If agent ended first AND call was fully resolved → OK, note it
- If call dropped → flag; if no callback within 10min → Critical concern

AGE CONCERN: If customer sounds young AND requests phones, adult products, or account changes → flag for human review

OUTSIDE WORK: If agent hints at working privately for customer, giving personal contact, or bypassing Proclick → Critical flag immediately

Respond ONLY with this exact JSON:

{{
  "overall_score": 0-100,
  "confidence": 0-100,
  "status": "Passed or Review or Critical",
  "requires_human_review": true/false,
  "human_review_reason": "reason or empty",
  "category_scores": {{
    "accuracy_and_information": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "customer_service_quality": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "active_listening": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "appropriate_actions": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "compliance_and_handling": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "emotion_management": {{"score": 0-100, "evidence": "emotion went from {emotion.get('customer_emotion_start')} to {emotion.get('customer_emotion_end')}", "passed": true/false}},
    "script_and_language": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "documentation_quality": {{"score": 0-100, "evidence": "assess notes quality and accuracy", "passed": true/false}},
    "audio_and_technical": {{"score": 0-100, "evidence": "...", "passed": true/false}},
    "call_closure": {{"score": 0-100, "evidence": "...", "passed": true/false}}
  }},
  "notes_score": 0-100,
  "notes_feedback": "specific feedback on note quality and any mismatches with actual call",
  "rules_evaluation": [
    {{
      "rule": "exact rule text",
      "category": "category",
      "severity": "Critical/Warning/Info",
      "passed": true/false,
      "confidence": 0-100,
      "evidence": "specific quote or observation"
    }}
  ],
  "flags": [
    {{
      "severity": "Critical or Warning",
      "type": "flag type",
      "title": "short title",
      "description": "what happened and why it matters",
      "timestamp": "time if known"
    }}
  ],
  "emotion_delta": {{
    "improved": true/false,
    "start": "{emotion.get('customer_emotion_start','Unknown')}",
    "end": "{emotion.get('customer_emotion_end','Unknown')}",
    "summary": "one sentence on emotional arc"
  }},
  "age_concern": {{"flagged": true/false, "reason": ""}},
  "call_end_assessment": "brief assessment of how/why call ended",
  "coaching_notes": "2-3 specific actionable coaching points",
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

    result = json.loads(text.strip())
    print(f"[Claude] Score: {result['overall_score']}% | Confidence: {result.get('confidence',0)}% | Notes: {result.get('notes_score',0)}%")
    return result

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def analyze_call(audio_path, agent_name, call_id=None, call_end_first='customer',
                 call_notes='', account_name='', line_issues='none', call_dropped=False):

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
        line_issues=line_issues
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
