import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

ACTION_WORDS = {
    "add", "answer", "bring", "buy", "call", "check", "complete", "create",
    "do", "draft", "email", "finish", "fix", "implement", "make", "meet",
    "prepare", "read", "remind", "review", "schedule", "send", "study",
    "submit", "turn in", "update", "write"
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6
}

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
      "assigner": "Who assigned or sent this task. For Google Classroom use 'Course Name: Teacher Name'. For email/docs use only the person or people names, not a course/prefix.",
      "priority": "low | normal | high",
      "is_all_day": boolean,
      "confidence": 0-100,
      "reasoning": "Briefly explain why this is a task"
    }}

    GUIDELINES:
    - Use 'reminder' for simple alerts (e.g., "Remind me to call Mom", "Alert me at 5pm").
    - Use 'task' for actionable work or assignments (e.g., "Finish the report", "Submit homework").
    - Extract the assigner separately from the assignee. The assignee is who should do the work; the assigner is who gave/sent the task.
    - For Google Classroom content, format assigner as "Class/Course Name: Teacher Name" when both are available.
    - For emails or uploaded documents, if an assigner appears as "Label: Person", remove the label and keep only "Person". If multiple assigners appear, join their names with commas.
    - If a time range or date range is provided (e.g. "3pm to 10pm", "Monday to Wednesday"), use the start for due_date and end for end_date.
    - IMPORTANT: Resolve relative dates (e.g., "tomorrow", "this Friday", "next Saturday") into absolute ISO 8601 dates using the provided Current Local Timestamp.
    - If no specific time is mentioned (e.g., "Buy groceries on Friday"), set "is_all_day" to true.
    
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
    
    # Also check if the day of the week (e.g., "Saturday") is in the text
    try:
        dt = datetime.fromisoformat(extracted_date.replace('Z', ''))
        if dt.strftime('%A').lower() in raw_text.lower():
            return True
    except:
        pass
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

def parse_current_time(current_time: Optional[str]) -> datetime:
    if not current_time:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    cleaned = current_time.replace("(Local Time)", "").strip()
    cleaned = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None)

def parse_time_fragment(text: str):
    match = re.search(r'\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text, re.IGNORECASE)
    if not match:
        return 12, 0, True

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3).lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute, False

def parse_due_date(text: str, now: datetime):
    lowered = text.lower()
    hour, minute, is_all_day = parse_time_fragment(lowered)
    target = None

    if re.search(r'\btoday\b', lowered):
        target = now
    elif re.search(r'\btomorrow\b', lowered):
        target = now + timedelta(days=1)
    else:
        next_weekday = re.search(r'\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', lowered)
        weekday = next_weekday or re.search(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', lowered)
        if weekday:
            desired = WEEKDAYS[weekday.group(1)]
            days_ahead = desired - now.weekday()
            if days_ahead <= 0 or next_weekday:
                days_ahead += 7
            target = now + timedelta(days=days_ahead)

    numeric = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b', lowered)
    if numeric:
        month = int(numeric.group(1))
        day = int(numeric.group(2))
        year = int(numeric.group(3) or now.year)
        if year < 100:
            year += 2000
        target = datetime(year, month, day)

    month_name = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:,\s*(\d{4}))?\b',
        lowered
    )
    if month_name:
        target = datetime(
            int(month_name.group(3) or now.year),
            MONTHS[month_name.group(1)],
            int(month_name.group(2))
        )

    if not target:
        return None, None, True

    due = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return due.strftime("%Y-%m-%dT%H:%M:%SZ"), None, is_all_day

def clean_task_title(text: str) -> str:
    title = re.sub(r'\b(?:by|due|on|at)\s+.*$', '', text, flags=re.IGNORECASE).strip()
    title = re.sub(r'^(please\s+|remind me to\s+|remind me\s+|i need to\s+|need to\s+)', '', title, flags=re.IGNORECASE)
    return title[:1].upper() + title[1:] if title else "New Task"

def local_nlp_extract_tasks(text: str, current_time: Optional[str] = None) -> List[Dict[str, Any]]:
    now = parse_current_time(current_time)
    candidates = []
    for chunk in re.split(r'[\n;]+|(?<=[.!?])\s+', text):
        candidate = chunk.strip(" -\t\r\n")
        if len(candidate) < 3:
            continue
        lowered = candidate.lower()
        if not any(word in lowered for word in ACTION_WORDS) and not re.search(r'\b(due|deadline|tomorrow|today|next\s+\w+)\b', lowered):
            continue
        candidates.append(candidate)

    if not candidates and text.strip():
        candidates = [text.strip()]

    tasks = []
    for candidate in candidates:
        lowered = candidate.lower()
        due_date, end_date, is_all_day = parse_due_date(candidate, now)
        item_type = "reminder" if "remind" in lowered or "alert" in lowered else "task"
        priority = "normal"
        if re.search(r'\b(urgent|asap|important|must|deadline)\b', lowered):
            priority = "high"
        elif re.search(r'\b(optional|low priority|when you can)\b', lowered):
            priority = "low"

        confidence = 74
        if due_date:
            confidence += 10
        if any(word in lowered for word in ACTION_WORDS):
            confidence += 8

        tasks.append(format_for_frontend({
            "item_type": item_type,
            "title": clean_task_title(candidate),
            "description": "Parsed locally when AI extraction was unavailable.",
            "due_date": due_date,
            "end_date": end_date,
            "assignee": "me",
            "is_all_day": is_all_day,
            "priority": priority,
            "confidence": min(confidence, 92)
        }))

    return tasks

def extract_task_from_text(text: str, current_time: Optional[str] = None) -> list:
    if not text or not text.strip():
        return []

    try:
        if not client:
            return local_nlp_extract_tasks(text, current_time)

        # Use provided local time or fallback to server UTC
        now_iso = current_time if current_time else datetime.now(timezone.utc).isoformat()
        prompt = build_prompt(text, now_iso)

        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
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
        return local_nlp_extract_tasks(text, current_time)

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
        "assigner": task.get("assigner") or task.get("assigned_by") or task.get("teacher") or task.get("sender"),
        "is_all_day": bool(task.get("is_all_day", False)),
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
    task["time"] = "All Day" if task.get("is_all_day") else (due_dt.strftime("%I:%M %p") if due_dt else "No time")
    return task
