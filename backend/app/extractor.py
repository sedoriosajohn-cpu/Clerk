import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(
    api_key=api_key,
    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45")),
    max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "1"))
) if api_key else None

MAX_AI_INPUT_CHARS = int(os.getenv("EXTRACTOR_MAX_AI_INPUT_CHARS", "18000"))
MAX_AI_CHUNKS = int(os.getenv("EXTRACTOR_MAX_AI_CHUNKS", "3"))
CHUNK_OVERLAP_CHARS = 500

ACTION_WORDS = {
    "add", "answer", "bring", "buy", "call", "check", "complete", "create",
    "do", "draft", "email", "finish", "fix", "implement", "make", "meet",
    "prepare", "read", "remind", "review", "schedule", "send", "study",
    "submit", "turn in", "update", "write"
}

TASK_HINT_RE = re.compile(
    r'\b('
    r'add|answer|bring|buy|call|check|complete|create|do|draft|email|finish|fix|'
    r'implement|make|meet|prepare|read|remind|review|schedule|send|study|submit|'
    r'turn\s+in|update|write|assignment|homework|project|quiz|test|exam|essay|'
    r'presentation|deadline|due|today|tomorrow|next\s+\w+|monday|tuesday|'
    r'wednesday|thursday|friday|saturday|sunday|jan(?:uary)?|feb(?:ruary)?|'
    r'mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|'
    r'oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}[/-]\d{1,2}'
    r')\b',
    re.IGNORECASE
)

NOISE_LINE_RE = re.compile(
    r'^\s*(?:page\s+\d+|\d+|copyright|table of contents|references)\s*$',
    re.IGNORECASE
)

STRONG_ACTION_RE = re.compile(
    r'\b(submit|finish|complete|turn\s+in|write|create|prepare|review|send|'
    r'schedule|call|email|buy|bring|read|study|fix|implement|make)\b',
    re.IGNORECASE
)

DEADLINE_RE = re.compile(r'\b(due|deadline|by|before|no later than)\b', re.IGNORECASE)
AMBIGUITY_RE = re.compile(
    r'\b(maybe|might|possibly|probably|optional|if you can|when you can|sometime|'
    r'eventually|consider|think about|maybe later)\b',
    re.IGNORECASE
)
EXPLICIT_TIME_RE = re.compile(
    r'\b(?:at\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b',
    re.IGNORECASE
)
DATE_REFERENCE_RE = re.compile(
    r'\b(today|tomorrow|next\s+\w+|monday|tuesday|wednesday|thursday|friday|'
    r'saturday|sunday|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
    r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|'
    r'nov(?:ember)?|dec(?:ember)?|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b',
    re.IGNORECASE
)

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

def clamp_score(score: float) -> int:
    return int(max(0, min(100, round(score))))

def parse_task_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        cleaned = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', str(value))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None

def title_terms(title: Optional[str]) -> List[str]:
    terms = re.findall(r'[a-z0-9]{3,}', str(title or "").lower())
    stop_words = {"the", "and", "for", "with", "task", "new", "due", "assignment"}
    return [term for term in terms if term not in stop_words]

def evidence_window(source_text: str, task: Dict[str, Any], radius: int = 500) -> str:
    if not source_text:
        return ""

    lowered = source_text.lower()
    candidates = title_terms(task.get("title"))
    for term in candidates[:5]:
        index = lowered.find(term)
        if index >= 0:
            start = max(0, index - radius)
            end = min(len(source_text), index + radius)
            return source_text[start:end]

    due_date = due_day_key_from_iso(task.get("due_date"))
    if due_date:
        index = lowered.find(due_date.lower())
        if index >= 0:
            start = max(0, index - radius)
            end = min(len(source_text), index + radius)
            return source_text[start:end]

    return source_text[: min(len(source_text), radius * 2)]

def due_day_key_from_iso(due_date: Optional[str]) -> str:
    if not due_date:
        return ""
    match = re.search(r'\d{4}-\d{2}-\d{2}', str(due_date))
    return match.group(0) if match else ""

def compact_text_for_extraction(text: str, max_chars: int = MAX_AI_INPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    normalized = re.sub(r'[ \t]+', ' ', text)
    lines = [line.strip() for line in normalized.splitlines()]
    kept = []
    seen = set()

    for index, line in enumerate(lines):
        if not line or NOISE_LINE_RE.match(line):
            continue
        if not TASK_HINT_RE.search(line):
            continue

        start = max(0, index - 1)
        end = min(len(lines), index + 2)
        segment = " ".join(part for part in lines[start:end] if part and not NOISE_LINE_RE.match(part))
        segment = re.sub(r'\s+', ' ', segment).strip()
        if segment and segment not in seen:
            seen.add(segment)
            kept.append(segment)

    if kept:
        compacted = "\n".join(kept)
        chunk_budget = max_chars * MAX_AI_CHUNKS
        if len(compacted) <= chunk_budget:
            return compacted
        return compacted[:chunk_budget]

    # Keep the beginning and end when no hints are found; syllabi often put
    # summary instructions up front and late-semester deadlines near the end.
    half = max_chars // 2
    return f"{text[:half]}\n\n{text[-half:]}"

def split_text_for_ai(text: str) -> List[str]:
    compacted = compact_text_for_extraction(text)
    if len(compacted) <= MAX_AI_INPUT_CHARS:
        return [compacted]

    chunks = []
    start = 0
    chunk_size = MAX_AI_INPUT_CHARS
    while start < len(compacted) and len(chunks) < MAX_AI_CHUNKS:
        end = min(len(compacted), start + chunk_size)
        chunks.append(compacted[start:end])
        if end == len(compacted):
            break
        start = max(end - CHUNK_OVERLAP_CHARS, start + 1)
    return chunks

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

        task = {
            "item_type": item_type,
            "title": clean_task_title(candidate),
            "description": "Parsed locally when AI extraction was unavailable.",
            "due_date": due_date,
            "end_date": end_date,
            "assignee": "me",
            "is_all_day": is_all_day,
            "priority": priority,
            "confidence": 68
        }
        tasks.append(format_for_frontend(
            adjust_confidence(candidate, task, current_time=current_time, date_verified=True)
        ))

    return tasks

def extract_task_from_text(text: str, current_time: Optional[str] = None) -> list:
    if not text or not text.strip():
        return []

    try:
        if not client:
            return local_nlp_extract_tasks(text, current_time)

        # Use provided local time or fallback to server UTC. Long documents are
        # condensed first so extraction time is based on likely task content,
        # not every policy paragraph or page footer in the source.
        now_iso = current_time if current_time else datetime.now(timezone.utc).isoformat()
        raw_tasks = []
        for chunk in split_text_for_ai(text):
            prompt = build_prompt(chunk, now_iso)
            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500"))
            )

            raw_content = response.choices[0].message.content
            raw_tasks.extend(extract_json(raw_content))
        
        processed_tasks = []
        for t in raw_tasks:
            valid = validate_task(t)
            
            is_verified = verify_with_regex(text, valid.get("due_date"))
            with_conf = adjust_confidence(
                text,
                valid,
                current_time=current_time,
                date_verified=is_verified
            )
            if valid.get("due_date") and not is_verified:
                with_conf["description"] += " (Warning: Date not explicitly found in source)"
                
            processed_tasks.append(format_for_frontend(with_conf))
            
        return processed_tasks
        
    except Exception as e:
        print(f"EXTRACTION ERROR: {e}")
        return local_nlp_extract_tasks(text, current_time)

def adjust_confidence(
    user_input: str,
    task: Dict[str, Any],
    current_time: Optional[str] = None,
    date_verified: bool = True
) -> Dict[str, Any]:
    model_score = max(0, min(100, int(task.get("confidence", 70))))
    score = 42 + (model_score * 0.32)
    evidence = evidence_window(user_input, task)
    evidence_lower = evidence.lower()
    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip()

    # Action clarity: strong verbs and explicit deadline language are more
    # reliable than vague nouns that merely look task-like.
    if STRONG_ACTION_RE.search(f"{title} {description}") or STRONG_ACTION_RE.search(evidence):
        score += 13
    elif any(word in evidence_lower for word in ACTION_WORDS):
        score += 7

    if DEADLINE_RE.search(evidence):
        score += 7
    if DATE_REFERENCE_RE.search(evidence):
        score += 5

    # Field completeness and source grounding.
    meaningful_title_terms = title_terms(title)
    if len(meaningful_title_terms) >= 2:
        score += 6
    elif title and title.lower() not in {"new task", "untitled task"}:
        score += 3
    else:
        score -= 16

    if description and description.lower() not in {"none", "null"}:
        score += 3
    if task.get("assigner") and str(task.get("assigner")).lower() not in {"none", "null", "me"}:
        score += 4

    matched_terms = sum(1 for term in meaningful_title_terms[:5] if term in evidence_lower)
    if matched_terms >= 2:
        score += 8
    elif meaningful_title_terms:
        score -= 4

    # Time quality: a task with a precise, future due time is much safer than
    # a guessed all-day date or a date that has already passed.
    now = parse_current_time(current_time)
    due_dt = parse_task_datetime(task.get("due_date"))
    end_dt = parse_task_datetime(task.get("end_date"))
    if due_dt:
        score += 10
        if task.get("is_all_day"):
            score += 2
        else:
            score += 7
            if EXPLICIT_TIME_RE.search(evidence):
                score += 5
            else:
                score -= 3

        days_until_due = (due_dt - now).total_seconds() / 86400
        if days_until_due < -1:
            score -= 22
        elif days_until_due < 0:
            score -= 8
        elif days_until_due <= 14:
            score += 5
        elif days_until_due <= 180:
            score += 2
        elif days_until_due > 730:
            score -= 10

        if date_verified:
            score += 8
        else:
            score -= 24
    else:
        if task.get("item_type") == "reminder":
            score -= 8
        else:
            score -= 4

    if end_dt:
        score += 4
        if due_dt and end_dt < due_dt:
            score -= 18

    # Ambiguous language and low-priority optionality are real uncertainty,
    # not just lower urgency.
    ambiguity_hits = len(AMBIGUITY_RE.findall(evidence))
    if ambiguity_hits:
        score -= min(24, 10 + (ambiguity_hits * 5))

    if task.get("priority") == "high" and DEADLINE_RE.search(evidence):
        score += 3
    elif task.get("priority") == "low" and ambiguity_hits:
        score -= 4

    task["confidence"] = clamp_score(score)
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
