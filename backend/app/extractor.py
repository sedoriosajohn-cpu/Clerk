import os
import json
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Check for API Key at startup
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

def build_prompt(user_input: str, current_time: str) -> str:
    return f"""
    You are an AI task extraction engine for a productivity app called Clerk.
    Current UTC Timestamp: {current_time}
    Extract ONE actionable task from the user's input.
    Return ONLY valid JSON in this exact format:
    {{
      "title": "short task title",
      "description": "expanded description",
      "due_date": "ISO 8601 timestamp in UTC or null",
      "assignee": "name or null",
      "priority": "low | normal | high",
      "confidence": 0
    }}
    User input: {user_input}
    """

def extract_json(text: str) -> Dict[str, Any]:
    trimmed = text.strip()
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        # Improved Regex to handle AI markdown blocks
        match = re.search(r'\{[\s\S]*\}', trimmed)
        if not match:
            raise ValueError(f"Model returned invalid format: {trimmed[:50]}...")
        return json.loads(match.group(0))
    
def normalize_priority(priority: str) -> str:
    if not priority: return "normal"
    p = str(priority).lower().strip()
    if p in ["high", "urgent", "important"]: return "high"
    if p in ["low", "minor"]: return "low"
    return "normal"

def validate_task(task: Dict[str, Any]) -> Dict[str, Any]:
    title = str(task.get("title", "Untitled Task")).strip()
    description = str(task.get("description", "")).strip()
    
    try:
        confidence = int(float(task.get("confidence", 50)))
    except (ValueError, TypeError):
        confidence = 50

    return {
        "title": title,
        "description": description,
        "due_date": task.get("due_date"),
        "assignee": task.get("assignee") if task.get("assignee") else "me",
        "priority": normalize_priority(task.get("priority")),
        "confidence": max(0, min(100, confidence))
    }

def adjust_confidence(user_input: str, task: Dict[str, Any]) -> Dict[str, Any]:
    text = user_input.lower()
    score = task["confidence"]
    
    if any(word in text for word in ["submit", "send", "finish", "due", "must"]):
        score += 5
    if any(word in text for word in ["maybe", "might", "should", "probably"]):
        score -= 15
        
    task["confidence"] = max(0, min(100, score))
    return task

def format_for_frontend(task: Dict[str, Any]) -> Dict[str, Any]:
    due_dt = None
    if task.get("due_date") and isinstance(task["due_date"], str):
        try:
            clean_date = task["due_date"].replace('Z', '+00:00')
            due_dt = datetime.fromisoformat(clean_date)
        except Exception as e:
            print(f"Date parsing error: {e}")
            
    task["due"] = due_dt.strftime("%m/%d/%Y") if due_dt else "No due date"
    task["time"] = due_dt.strftime("%I:%M %p") if due_dt else "No time"
    return task

def extract_task_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text or not text.strip():
        return None

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        prompt = build_prompt(text, now_iso)

        response = client.chat.completions.create(
            model="gpt-5.4", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        raw_content = response.choices[0].message.content
        
        task = extract_json(raw_content)
        task = validate_task(task)
        task = adjust_confidence(text, task)
        return format_for_frontend(task)
        
    except Exception as e:
        print(f"EXTRACTION ERROR: {e}")
        return None