import os
import json
import sqlite3
import base64
import anthropic
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ─── SETUP API CLIENTS ───────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

DB_PATH = os.path.join(os.getenv('HOME', '.'), 'voiceguard.db')

# ─── LOAD ACTIVE RULES FROM DATABASE ─────────────────────────────────────────
def load_active_rules():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rules = conn.execute(
        "SELECT description, category, severity FROM rules WHERE active=1 ORDER BY severity DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rules]

# ─── STEP 1: GEMINI AUDIO ANALYSIS ───────────────────────────────────────────
def analyze_audio_with_gemini(audio_path):
    """
    Send audio file to Gemini 2.0 Flash for emotion and tone analysis.
    Returns transcript + emotion data.
    """
    print(f"[Gemini] Analyzing audio: {audio_path}")

    model = genai.GenerativeModel('gemini-2.5-flash')

    # Read and encode audio file
    with open(audio_path, 'rb') as f:
        audio_data = f.read()

    audio_b64 = base64.b64encode(audio_data).decode('utf-8')

    # Detect mime type
    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {
        '.mp3': 'audio/mp3',
        '.wav': 'audio/wav',
        '.m4a': 'audio/mp4',
        '.ogg': 'audio/ogg',
        '.flac': 'audio/flac',
        '.webm': 'audio/webm',
    }
    mime_type = mime_map.get(ext, 'audio/mp3')

    prompt = """You are an expert call center audio analyst. Listen to this customer service call carefully.

Provide a detailed analysis in the following JSON format ONLY — no other text:

{
  "transcript": "Full word-for-word transcript. Label each line as AGENT: or CUSTOMER:",
  "duration_estimate": "Estimated call duration e.g. 5:32",
  "emotion_analysis": {
    "customer_emotion": "One of: Happy, Satisfied, Neutral, Frustrated, Angry, Confused",
    "customer_frustration_level": 0-100,
    "customer_satisfaction_level": 0-100,
    "agent_tone": "One of: Professional, Friendly, Neutral, Rushed, Defensive, Robotic",
    "agent_stress_level": 0-100,
    "agent_speaking_speed": "One of: Too Fast, Normal, Too Slow",
    "key_emotional_moments": [
      {
        "timestamp": "approximate time e.g. 2:14",
        "description": "What happened emotionally at this moment"
      }
    ],
    "long_silences": true or false,
    "agent_interrupted_customer": true or false,
    "customer_raised_voice": true or false
  },
  "summary": "2-3 sentence summary of what the call was about and how it went"
}"""

    response = model.generate_content([
        {'mime_type': mime_type, 'data': audio_b64},
        prompt
    ])

    # Parse JSON response
    text = response.text.strip()
    # Clean up markdown if present
    if text.startswith('```'):
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    result = json.loads(text)
    print(f"[Gemini] Analysis complete. Customer emotion: {result['emotion_analysis']['customer_emotion']}")
    return result

# ─── STEP 2: CLAUDE QA SCORING ────────────────────────────────────────────────
def score_call_with_claude(gemini_result, rules):
    """
    Send transcript + emotion data + rules to Claude for full QA scoring.
    Returns complete scorecard with scores and evidence.
    """
    print(f"[Claude] Scoring call against {len(rules)} rules...")

    transcript = gemini_result.get('transcript', '')
    emotion = gemini_result.get('emotion_analysis', {})
    summary = gemini_result.get('summary', '')

    # Build rules text
    rules_text = '\n'.join([
        f"- [{r['severity'].upper()}] ({r['category']}) {r['description']}"
        for r in rules
    ])

    prompt = f"""You are an expert call center QA analyst. Evaluate this customer service call.

═══ CALL TRANSCRIPT ═══
{transcript}

═══ AUDIO EMOTION ANALYSIS (from audio AI) ═══
- Customer Emotion: {emotion.get('customer_emotion', 'Unknown')}
- Customer Frustration Level: {emotion.get('customer_frustration_level', 0)}/100
- Customer Satisfaction Level: {emotion.get('customer_satisfaction_level', 0)}/100
- Agent Tone: {emotion.get('agent_tone', 'Unknown')}
- Agent Stress Level: {emotion.get('agent_stress_level', 0)}/100
- Agent Speaking Speed: {emotion.get('agent_speaking_speed', 'Normal')}
- Customer Raised Voice: {emotion.get('customer_raised_voice', False)}
- Agent Interrupted Customer: {emotion.get('agent_interrupted_customer', False)}
- Long Silences Detected: {emotion.get('long_silences', False)}
- Key Emotional Moments: {json.dumps(emotion.get('key_emotional_moments', []))}

═══ CALL SUMMARY ═══
{summary}

═══ ACTIVE QA RULES (evaluate every single one) ═══
{rules_text}

═══ STANDARD QA CATEGORIES ═══
1. Accuracy of Information - Did agent provide correct information and follow policies?
2. Customer Service Quality - Was agent professional, empathetic, clear, and courteous?
3. Appropriate Actions - Did agent take correct actions, follow workflows, document properly?
4. Compliance & Call Handling - Did agent verify identity, follow compliance requirements?
5. Emotion & Tone - Based on audio analysis, how well did agent handle customer emotions?

Respond ONLY with this exact JSON format:

{{
  "overall_score": 0-100,
  "status": "Passed or Review or Critical",
  "category_scores": {{
    "accuracy_of_information": {{
      "score": 0-100,
      "evidence": "Specific evidence from the call",
      "passed": true or false
    }},
    "customer_service_quality": {{
      "score": 0-100,
      "evidence": "Specific evidence from the call",
      "passed": true or false
    }},
    "appropriate_actions": {{
      "score": 0-100,
      "evidence": "Specific evidence from the call",
      "passed": true or false
    }},
    "compliance_and_handling": {{
      "score": 0-100,
      "evidence": "Specific evidence from the call",
      "passed": true or false
    }},
    "emotion_and_tone": {{
      "score": 0-100,
      "evidence": "Based on audio analysis - specific observations",
      "passed": true or false
    }}
  }},
  "rules_evaluation": [
    {{
      "rule": "exact rule text",
      "category": "rule category",
      "severity": "Critical or Warning or Info",
      "passed": true or false,
      "evidence": "Specific quote or observation from the call that supports this evaluation"
    }}
  ],
  "flags": [
    {{
      "severity": "Critical or Warning",
      "title": "Short flag title",
      "description": "What happened and why it matters",
      "timestamp": "approximate time if known"
    }}
  ],
  "coaching_notes": "2-3 sentences of specific coaching feedback for this agent",
  "positive_highlights": "1-2 things the agent did well"
}}"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = message.content[0].text.strip()
    if text.startswith('```'):
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()

    result = json.loads(text)
    print(f"[Claude] Scoring complete. Overall score: {result['overall_score']}%")
    return result

# ─── MAIN ANALYZE FUNCTION ────────────────────────────────────────────────────
def analyze_call(audio_path, agent_name, call_id=None):
    """
    Full pipeline: audio → Gemini → Claude → scorecard
    Returns complete analysis result.
    """
    print(f"\n{'='*50}")
    print(f"[VoiceGuard] Starting analysis for agent: {agent_name}")
    print(f"[VoiceGuard] Audio file: {audio_path}")
    print(f"{'='*50}")

    try:
        # Step 1: Gemini audio analysis
        gemini_result = analyze_audio_with_gemini(audio_path)

        # Step 2: Load active rules
        rules = load_active_rules()
        print(f"[VoiceGuard] Loaded {len(rules)} active rules")

        # Step 3: Claude QA scoring
        claude_result = score_call_with_claude(gemini_result, rules)

        # Step 4: Build final result
        emotion = gemini_result['emotion_analysis']
        emotion_label = f"{_emotion_emoji(emotion['customer_emotion'])} {emotion['customer_emotion']}"

        final_result = {
            'call_id': call_id or f"CALL-{int(__import__('time').time())}",
            'agent_name': agent_name,
            'duration': gemini_result.get('duration_estimate', '--'),
            'overall_score': claude_result['overall_score'],
            'status': claude_result['status'],
            'emotion': emotion_label,
            'flags': len(claude_result.get('flags', [])),
            'transcript': gemini_result['transcript'],
            'summary': gemini_result['summary'],
            'scorecard': claude_result,
            'emotion_analysis': emotion,
        }

        # Step 5: Save to database
        _save_to_db(final_result)
        print(f"[VoiceGuard] ✅ Analysis complete and saved to database")
        return final_result

    except Exception as e:
        print(f"[VoiceGuard] ❌ Error during analysis: {str(e)}")
        raise e

def _emotion_emoji(emotion):
    emojis = {
        'Happy': '😊', 'Satisfied': '😊', 'Neutral': '😐',
        'Frustrated': '😤', 'Angry': '😠', 'Confused': '😕'
    }
    return emojis.get(emotion, '😐')

def _save_to_db(result):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        '''INSERT INTO calls (call_id, agent_name, duration, overall_score, emotion, status, flags, scorecard, transcript)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            result['call_id'],
            result['agent_name'],
            result['duration'],
            result['overall_score'],
            result['emotion'],
            result['status'],
            result['flags'],
            json.dumps(result['scorecard']),
            result['transcript']
        )
    )
    conn.commit()
    conn.close()

# ─── TEST MODE ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        audio_file = sys.argv[1]
        agent = sys.argv[2]
        call_id = sys.argv[3] if len(sys.argv) > 3 else None
        result = analyze_call(audio_file, agent, call_id)
        print(f"\n✅ RESULT:")
        print(f"   Score: {result['overall_score']}%")
        print(f"   Status: {result['status']}")
        print(f"   Emotion: {result['emotion']}")
        print(f"   Flags: {result['flags']}")
    else:
        print("Usage: python ai_engine.py <audio_file> <agent_name> [call_id]")
        print("Example: python ai_engine.py call.mp3 'Sarah K.' CALL-001")
