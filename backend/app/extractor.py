import os
import json
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

def build_prompt(user_input: str, current_time: str) -> str:
    return f"""
    You are the core extraction engine for 'Clerk', an AI task manager.
    Current Local Timestamp: {current_time}
    
    TASK:
    Extract EVERY actionable task, reminder, assignment, or deadline from the provided text.
    The text may come from a raw user message, a PDF syllabus, or a document. 
    Ignore page numbers, headers, and non-actionable information.

    DATA FORMAT:
    Return ONLY a valid JSON LIST of objects. Do not include markdown formatting like ```json.
    
    SCHEMA:
    {{
      "item_type": "task | reminder",
      "title": "Clear, concise task name",
      "description": "Any extra context found in the text",
      "due_date": "ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ) or null",
      "end_date": "ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ) or null",
      "assignee": "name or 'me'",
      "priority": "low | normal | high",
      "confidence": 0-100,
      "reasoning": "Briefly explain why this is a task"
    }}

    GUIDELINES:
    - Use 'reminder' for simple alerts (e.g., "Remind me to call Mom", "Alert me at 5pm").
    - Use 'task' for actionable work or assignments (e.g., "Finish the report", "Submit homework").
    - If a time range is provided (e.g. "3pm to 10pm"), use the start for due_date and end for end_date.
    
    INPUT TEXT:
    {user_input}
    """

def verify_with_regex(raw_text: str, extracted_date: str) -> bool:
    """
    Fact-checks the AI by looking for the extracted date string 
    inside the raw document text using basic regex patterns.
    """
    if not extracted_date:
        return True # Nothing to verify
    
    # Extract just the YYYY-MM-DD part
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', extracted_date)
    if not date_match:
        return True
    
    target = date_match.group(1)
    # Check for common variations: 2026-05-12, 05/12, May 12
    # This is a 'soft' check to ensure the date actually exists in the source
    if target in raw_text or target.replace('-', '/') in raw_text:
        return True
    
    return False

def extract_json(text: str) -> list:
    trimmed = text.replace('```json', '').replace('```', '').strip()
    try:
        data = json.loads(trimmed)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        match = re.search(r'\[[\s\S]*\]', trimmed)
        if match:
            return json.loads(match.group(0))
        obj_match = re.search(r'\{[\s\S]*\}', trimmed)
        if obj_match:
            return [json.loads(obj_match.group(0))]
        raise ValueError("Failed to parse AI response as JSON")

def extract_task_from_text(text: str, current_time: Optional[str] = None) -> list:
    if not text or not text.strip():
        return []

    try:
        # Use provided local time or fallback to server UTC
        now_iso = current_time if current_time else datetime.now(timezone.utc).isoformat()
        prompt = build_prompt(text, now_iso)

        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        raw_content = response.choices[0].message.content
        raw_tasks = extract_json(raw_content)
        
        processed_tasks = []
        for t in raw_tasks:
            valid = validate_task(t)
            
            # Hybrid Confidence Check
            # 1. Keyword Heuristics
            with_conf = adjust_confidence(text, valid)
            
            # 2. Regex Anchor Verification (Fact-Checking)
            is_verified = verify_with_regex(text, valid.get("due_date"))
            if not is_verified:
                with_conf["confidence"] = int(with_conf["confidence"] * 0.5) # Penalty for hallucination risk
                with_conf["description"] += " (Warning: Date not explicitly found in source)"
                
            processed_tasks.append(format_for_frontend(with_conf))
            
        return processed_tasks
        
    except Exception as e:
        print(f"EXTRACTION ERROR: {e}")
        return []

def adjust_confidence(user_input: str, task: Dict[str, Any]) -> Dict[str, Any]:
    text = user_input.lower()
    score = task["confidence"]
    
    # Intent keywords
    if any(word in text for word in ["submit", "must", "due", "deadline", "assignment"]):
        score += 10
    if any(word in text for word in ["maybe", "might", "probably", "optional"]):
        score -= 20
        
    # Document-specific grounding
    if "syllabus" in text or "course" in text:
        score += 5
        
    task["confidence"] = max(0, min(100, score))
    return task

def validate_task(task: Dict[str, Any]) -> Dict[str, Any]:
    # Ensure title exists
    title = str(task.get("title", "New Task")).strip()
    if not title or title == "null": title = "Untitled Task"
    
    # Standardize Priority
    p = str(task.get("priority", "normal")).lower()
    if p in ["high", "urgent"]: p = "high"
    elif p in ["low", "minor"]: p = "low"
    else: p = "normal"

    return {
        "item_type": str(task.get("item_type", "task")).lower(),
        "title": title,
        "description": str(task.get("description", "")),
        "due_date": task.get("due_date"),
        "end_date": task.get("end_date"),
        "assignee": task.get("assignee") if task.get("assignee") else "me",
        "priority": p,
        "confidence": int(task.get("confidence", 70))
    }

def format_for_frontend(task: Dict[str, Any]) -> Dict[str, Any]:
    due_dt = None
    if task.get("due_date"):
        try:
            # Strip 'Z' or offsets to treat as "Wall Time" (local naive datetime)
            # This ensures the backend-generated time string matches the extracted wall time
            clean_date = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', task["due_date"])
            due_dt = datetime.fromisoformat(clean_date)
        except:
            pass
            
    task["due"] = due_dt.strftime("%m/%d/%Y") if due_dt else "No due date"
    task["time"] = due_dt.strftime("%I:%M %p") if due_dt else "No time"
    return task