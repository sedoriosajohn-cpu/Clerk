import os
import json
import re
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_AI_WORKERS = int(os.getenv("EXTRACTOR_MAX_AI_WORKERS", "3"))
SCHEDULE_IMAGE_MAX_SIDE = int(os.getenv("SCHEDULE_IMAGE_MAX_SIDE", "2048"))
SCHEDULE_IMAGE_QUALITY = int(os.getenv("SCHEDULE_IMAGE_QUALITY", "86"))
SCHEDULE_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_SCHEDULE_MAX_OUTPUT_TOKENS", "1800"))
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
    except (ValueError, AttributeError):
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

def extract_json_from_chunk(chunk: str, now_iso: str) -> list:
    prompt = build_prompt(chunk, now_iso)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_completion_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500"))
    )
    return extract_json(response.choices[0].message.content)

def optimize_schedule_image(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    try:
        from PIL import Image, ImageOps
        from io import BytesIO
    except Exception:
        return image_bytes, mime_type

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((SCHEDULE_IMAGE_MAX_SIDE, SCHEDULE_IMAGE_MAX_SIDE))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            output = BytesIO()
            image.save(output, format="JPEG", quality=SCHEDULE_IMAGE_QUALITY, optimize=True)
            return output.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, mime_type

def normalize_schedule_name(value: Optional[str]) -> str:
    text = re.sub(r'[^a-z\s,]', ' ', str(value or "").lower())
    text = re.sub(r'\s+', ' ', text).strip()
    if "," in text:
        last, first = [part.strip() for part in text.split(",", 1)]
        text = f"{first} {last}".strip()
    return text

def schedule_names_match(row_name: Optional[str], target_names: List[str]) -> bool:
    normalized_row = normalize_schedule_name(row_name)
    if not normalized_row:
        return True

    row_tokens = set(normalized_row.split())
    if not row_tokens:
        return True

    for name in target_names:
        normalized_target = normalize_schedule_name(name)
        target_tokens = set(normalized_target.split())
        if not target_tokens:
            continue
        if normalized_row == normalized_target:
            return True
        if len(row_tokens & target_tokens) >= min(2, len(target_tokens)):
            return True
    return False

def parse_schedule_weekly_hours(value: Any) -> Optional[float]:
    match = re.search(r'\d+(?:\.\d+)?', str(value or ""))
    return float(match.group(0)) if match else None

def parse_schedule_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        cleaned = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', str(value))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None

def schedule_shift_hours(task: Dict[str, Any]) -> float:
    start = parse_schedule_datetime(task.get("due_date"))
    end = parse_schedule_datetime(task.get("end_date"))
    if not start or not end:
        return 0
    if end < start:
        end += timedelta(days=1)
    return max(0, (end - start).total_seconds() / 3600)

def extracted_schedule_total_hours(tasks: List[Dict[str, Any]]) -> float:
    return round(sum(schedule_shift_hours(task) for task in tasks), 2)

def parse_schedule_day_label(label: str, current_time: Optional[str]) -> Optional[datetime]:
    now = parse_current_time(current_time)
    weekday_match = re.fullmatch(r'\s*(sun|mon|tue|wed|thu|fri|sat)(?:day)?\s*', str(label or ""), re.IGNORECASE)
    if weekday_match:
        # Map to Python weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
        py_weekday = {"sun": 6, "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5}[weekday_match.group(1).lower()[:3]]
        # Find the corresponding day in the Sun-Sat calendar week that contains `now`.
        # The ±3-day heuristic breaks for Saturday when current_time is Mon-Tue.
        now_offset_from_sun = (now.weekday() + 1) % 7  # Sun=0, Mon=1, ..., Sat=6
        week_sunday = (now - timedelta(days=now_offset_from_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
        target_offset = (py_weekday + 1) % 7            # Sun=0, Mon=1, ..., Sat=6
        return week_sunday + timedelta(days=target_offset)

    match = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\D+(\d{1,2})\b',
        str(label or ""),
        re.IGNORECASE
    )
    if not match:
        return None

    month = MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = now.year
    candidate = datetime(year, month, day)
    if candidate < now - timedelta(days=300):
        candidate = datetime(year + 1, month, day)
    elif candidate > now + timedelta(days=300):
        candidate = datetime(year - 1, month, day)
    return candidate

def parse_schedule_clock(hour_text: str, minute_text: Optional[str], suffix: str) -> tuple[int, int]:
    hour = int(hour_text)
    minute = int(minute_text or 0)
    suffix = suffix.lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute

def row_cell_to_task(
    day_label: str,
    cell_text: Any,
    matched_row_name: Optional[str],
    row_weekly_hours: Optional[Any],
    current_time: Optional[str]
) -> Optional[Dict[str, Any]]:
    text = re.sub(r'\s+', ' ', str(cell_text or "")).strip()
    if not text or text.lower() in {"off", "none", "null", "n/a"}:
        return None

    time_match = re.search(
        r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)',
        text,
        re.IGNORECASE
    )
    date = parse_schedule_day_label(day_label, current_time)
    if not time_match or not date:
        return None

    start_hour, start_minute = parse_schedule_clock(time_match.group(1), time_match.group(2), time_match.group(3))
    end_hour, end_minute = parse_schedule_clock(time_match.group(4), time_match.group(5), time_match.group(6))
    start = date.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = date.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end < start:
        end += timedelta(days=1)

    role_text = text[time_match.end():].strip(" -:")
    title_role = role_text if role_text else ""
    title = f"Work shift - {title_role}" if title_role else "Work shift"
    return {
        "item_type": "reminder",
        "title": title,
        "description": f"Matched schedule row: {matched_row_name or 'employee schedule row'}.",
        "due_date": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_date": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "assignee": "me",
        "assigner": "Uploaded work schedule",
        "priority": "normal",
        "is_all_day": False,
        "confidence": 96,
        "matched_row_name": matched_row_name,
        "row_weekly_hours": row_weekly_hours,
        "raw_cell_text": text
    }

def row_cells_to_tasks(container: Dict[str, Any], current_time: Optional[str]) -> List[Dict[str, Any]]:
    row_cells = container.get("row_cells")
    if isinstance(row_cells, list):
        day_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        row_cells = {day_labels[index]: value for index, value in enumerate(row_cells[:7])}
    if not isinstance(row_cells, dict):
        return []

    tasks = []
    for day_label, cell_text in row_cells.items():
        task = row_cell_to_task(
            day_label,
            cell_text,
            container.get("matched_row_name"),
            container.get("row_weekly_hours"),
            current_time
        )
        if task:
            tasks.append(task)
    return tasks

def validate_schedule_extraction(tasks: List[Dict[str, Any]], target_names: List[str]) -> List[Dict[str, Any]]:
    if not tasks:
        return []

    row_names = [task.get("matched_row_name") for task in tasks if task.get("matched_row_name")]
    if os.getenv("REQUIRE_SCHEDULE_ROW_NAME", "1") == "1" and not row_names:
        print("[schedule] rejected: no matched_row_name in extracted tasks")
        return []
    if row_names and not any(schedule_names_match(row_name, target_names) for row_name in row_names):
        print(f"[schedule] rejected: row names {row_names} don't match target names {target_names}")
        return []

    row_hours = next(
        (parse_schedule_weekly_hours(task.get("row_weekly_hours")) for task in tasks if parse_schedule_weekly_hours(task.get("row_weekly_hours")) is not None),
        None
    )
    # Only require weekly hours when the env flag is explicitly set to "1" AND only
    # reject when the extracted total is implausibly wrong (>15% or >2 h off).
    if row_hours is not None:
        total_hours = extracted_schedule_total_hours(tasks)
        tolerance = max(2.0, row_hours * 0.15)
        if abs(total_hours - row_hours) > tolerance:
            print(f"[schedule] rejected: extracted {total_hours}h vs row total {row_hours}h (tolerance {tolerance:.1f}h)")
            return []

    # Reject unrealistically long single shifts (>16 h suggests a row-reading error).
    if any(schedule_shift_hours(task) >= 16 for task in tasks):
        print("[schedule] rejected: shift >= 16 hours detected")
        return []

    # Drop shifts that fall outside the 7-day window anchored on the earliest valid shift.
    # This removes stray dates from the previous/next week caused by grid-reading errors.
    valid_starts = sorted(
        dt for dt in (parse_schedule_datetime(t.get("due_date")) for t in tasks) if dt
    )
    if valid_starts:
        week_anchor = valid_starts[0]
        tasks = [
            t for t in tasks
            if (dt := parse_schedule_datetime(t.get("due_date"))) and abs((dt - week_anchor).days) <= 6
        ]

    unique = {}
    for task in tasks:
        key = (task.get("due_date"), task.get("end_date"))
        if key[0] and key[1]:
            unique[key] = task
    return sorted(unique.values(), key=lambda task: task.get("due_date") or "")

def encode_schedule_image(image, max_side: int = 2048) -> str:
    from PIL import ImageEnhance
    from io import BytesIO

    image = ImageEnhance.Contrast(image).enhance(1.35)
    image = ImageEnhance.Sharpness(image).enhance(1.55)
    if image.height < 180:
        image = image.resize((image.width * 3, image.height * 3))
    image.thumbnail((max_side, max_side))
    output = BytesIO()
    image.save(output, format="JPEG", quality=max(SCHEDULE_IMAGE_QUALITY, 92), optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")

def detect_schedule_grid(image) -> tuple[List[int], List[tuple[int, int]]]:
    from PIL import ImageOps

    gray = ImageOps.grayscale(image)
    width, height = gray.size
    pixels = gray.load()
    x0, x1 = int(width * 0.05), int(width * 0.95)

    dark_rows = []
    for y in range(int(height * 0.05), int(height * 0.86)):
        dark = 0
        total = 0
        for x in range(x0, x1, 4):
            total += 1
            if pixels[x, y] < 75:
                dark += 1
        if total and dark / total > 0.23:
            dark_rows.append(y)

    row_clusters = []
    for y in dark_rows:
        if not row_clusters or y - row_clusters[-1][-1] > 3:
            row_clusters.append([y])
        else:
            row_clusters[-1].append(y)
    horizontal_lines = [sum(cluster) // len(cluster) for cluster in row_clusters]

    if len(horizontal_lines) < 4:
        return [], []

    table_top, table_bottom = horizontal_lines[0], horizontal_lines[-1]
    dark_cols = []
    for x in range(int(width * 0.03), int(width * 0.97)):
        dark = 0
        total = 0
        for y in range(table_top, table_bottom, 3):
            total += 1
            if pixels[x, y] < 95:
                dark += 1
        if total and dark / total > 0.14:
            dark_cols.append(x)

    col_clusters = []
    for x in dark_cols:
        if not col_clusters or x - col_clusters[-1][-1] > 4:
            col_clusters.append([x])
        else:
            col_clusters[-1].append(x)
    candidates = [sum(cluster) // len(cluster) for cluster in col_clusters]

    best = []
    best_score = float("inf")
    for start in range(len(candidates)):
        stack = [([candidates[start]], start)]
        while stack:
            sequence, index = stack.pop()
            if len(sequence) == 8:
                gaps = [sequence[i + 1] - sequence[i] for i in range(7)]
                avg_gap = sum(gaps) / len(gaps)
                variance = sum((gap - avg_gap) ** 2 for gap in gaps) / len(gaps)
                score = (
                    variance
                    + abs(sequence[-1] - (width * 0.94)) * 0.8
                    + abs(sequence[0] - (width * 0.25)) * 0.2
                )
                if score < best_score:
                    best = sequence
                    best_score = score
                continue

            for next_index in range(index + 1, len(candidates)):
                gap = candidates[next_index] - sequence[-1]
                if gap > 190:
                    break
                if 115 <= gap <= 190:
                    stack.append((sequence + [candidates[next_index]], next_index))
    day_lines = best if len(best) >= 8 else []

    row_bounds = [
        (horizontal_lines[index], horizontal_lines[index + 1])
        for index in range(len(horizontal_lines) - 1)
        if 34 <= horizontal_lines[index + 1] - horizontal_lines[index] <= 95
    ]
    return day_lines, row_bounds

def build_row_contact_sheet(image, day_lines: List[int], row_bounds: List[tuple[int, int]]):
    from PIL import Image, ImageDraw

    if not day_lines or not row_bounds:
        return None

    name_left = max(0, day_lines[0] - int((day_lines[1] - day_lines[0]) * 1.95))
    name_right = day_lines[0]
    cell_width = name_right - name_left
    cell_height = 72
    label_width = 70
    sheet = Image.new("RGB", (label_width + cell_width * 2, cell_height * len(row_bounds)), "white")
    draw = ImageDraw.Draw(sheet)

    for index, (top, bottom) in enumerate(row_bounds):
        crop = image.crop((name_left, max(0, top - 3), name_right, min(image.height, bottom + 3)))
        crop = crop.resize((cell_width * 2, cell_height))
        y = index * cell_height
        draw.text((8, y + 24), f"{index}", fill="black")
        sheet.paste(crop, (label_width, y))
    return sheet

def locate_schedule_row_from_image(image, day_lines: List[int], row_bounds: List[tuple[int, int]], target_names: List[str]) -> Optional[Dict[str, Any]]:
    if not client:
        return None

    contact_sheet = build_row_contact_sheet(image, day_lines, row_bounds)
    if contact_sheet is None:
        return None

    encoded_contact = encode_schedule_image(contact_sheet, max_side=2200)
    prompt = f"""
    Find the row index for this employee in the labeled name-cell sheet:
    {json.dumps(target_names)}

    Return ONLY JSON:
    {{"row_index": 0, "matched_row_name": "Name as shown", "row_weekly_hours": "weekly hours as shown"}}
    If no row matches, return {{"row_index": null}}.
    """
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"),
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_contact}", "detail": "high"}}
            ]
        }],
        temperature=0,
        max_completion_tokens=300
    )
    extracted = extract_json(response.choices[0].message.content)
    if not extracted or not isinstance(extracted[0], dict):
        return None

    row_index = extracted[0].get("row_index")
    try:
        row_index = int(row_index)
    except (TypeError, ValueError):
        return None
    if row_index < 0 or row_index >= len(row_bounds):
        return None
    return {
        "row_index": row_index,
        "matched_row_name": extracted[0].get("matched_row_name"),
        "row_weekly_hours": extracted[0].get("row_weekly_hours")
    }

def extract_schedule_from_detected_row(image, row_info: Dict[str, Any], day_lines: List[int], row_bounds: List[tuple[int, int]], current_time: Optional[str]) -> List[Dict[str, Any]]:
    if not client:
        return []

    row_index = row_info["row_index"]
    top, bottom = row_bounds[row_index]
    day_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    header_top = max(0, min(row_bounds[0][0], top) - 70)
    header_bottom = max(header_top + 40, min(image.height, row_bounds[0][0] + 10))

    content = [{
        "type": "text",
        "text": f"""
        These are individual day cells from one employee row.
        Employee row: {row_info.get("matched_row_name") or "matched user"}
        Weekly hours: {row_info.get("row_weekly_hours") or "unknown"}
        Look at the date header strip first. Use the FULL printed date label for each column
        (e.g. "Mon Jun 8", "Tue Jun 9") — not just the weekday abbreviation.
        Read each cell image independently; empty cell means no shift.
        Do NOT copy text between cells or reuse a time from a different cell.
        Return ONLY a JSON object with matched_row_name, row_weekly_hours, row_cells (keys = full date
        label from header, values = exact cell text or ""), and shifts.
        """
    }]

    header = image.crop((day_lines[0], header_top, day_lines[-1], header_bottom))
    content.append({"type": "text", "text": "date header strip"})
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_schedule_image(header)}", "detail": "high"}})

    for index, label in enumerate(day_labels):
        cell = image.crop((
            day_lines[index],
            max(0, top - 2),
            day_lines[index + 1],
            min(image.height, bottom + 2)
        ))
        content.append({"type": "text", "text": f"{label} cell"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_schedule_image(cell)}", "detail": "high"}})

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": content}],
        temperature=0,
        max_completion_tokens=SCHEDULE_MAX_OUTPUT_TOKENS
    )

    extracted = extract_json(response.choices[0].message.content)
    if len(extracted) == 1 and isinstance(extracted[0], dict):
        container = extracted[0]
        container.setdefault("matched_row_name", row_info.get("matched_row_name"))
        container.setdefault("row_weekly_hours", row_info.get("row_weekly_hours"))
        return row_cells_to_tasks(container, current_time) or parse_schedule_image_response(response.choices[0].message.content, current_time)
    return parse_schedule_image_response(response.choices[0].message.content, current_time)

def extract_work_schedule_from_grid_image(image_bytes: bytes, target_names: List[str], current_time: Optional[str]) -> List[Dict[str, Any]]:
    try:
        from PIL import Image, ImageOps
        from io import BytesIO
    except Exception:
        return []

    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
        day_lines, row_bounds = detect_schedule_grid(image)
        if len(day_lines) < 8 or not row_bounds:
            return []
        row_info = locate_schedule_row_from_image(image, day_lines, row_bounds, target_names)
        if not row_info:
            return []
        tasks = extract_schedule_from_detected_row(image, row_info, day_lines, row_bounds, current_time)
        return validate_schedule_extraction(tasks, target_names)
    except Exception as exc:
        print(f"Grid schedule extraction failed: {exc}")
        return []

def build_schedule_image_prompt(target_names: List[str], now_iso: str, retry: bool = False) -> str:
    retry_rules = """
    STRICT RETRY:
    A previous read was rejected because it likely crossed into an adjacent row or the shift hours did not match the row's weekly-hours total.
    Re-locate the target name on the left, then trace one straight horizontal row across all day columns.
    Reject clearer-looking cells from neighboring rows, especially 8:00 AM shifts, unless they are in the matched row.
    Return [] if you cannot make the extracted shifts total the matched row's weekly-hours value.
    """ if retry else ""

    return f"""
    You are Clerk's work-schedule extraction engine.
    Current Local Timestamp: {now_iso}

    TASK:
    Read this weekly employee schedule image. Extract ONLY the work shifts for the logged-in user.
    Match the user by comparing schedule row names against these possible names:
    {json.dumps(target_names)}
    {retry_rules}

    IMPORTANT MATCHING RULES:
    - Schedules may write names as "Last, First" while the user profile may be "First Last".
    - Use the best matching employee row only. Do not extract shifts for rows directly above or below it.
    - Read across the SAME horizontal row as the matched name. Never take a cell from a neighboring row.
    - Each column maps to exactly one date. Use the printed day+date header (e.g. "Mon Jun 8") for that column.
    - Verify that the extracted shift durations roughly add up to the row's weekly-hours total.
    - If the totals conflict by more than 2 hours, re-read the row and correct the wrong cells.
    - Treat "3:00 PM - 10:30 PM" as an afternoon/evening shift, never as 8:00 AM.
    - An "x", checkmark, or blank means no shift for that day — do not inherit a time from another column.
    - If no row confidently matches the user, return [].
    - Use the week/date headers in the image for each shift date. Only output shifts within the printed week range.
    - Ignore handwritten annotations unless they are clearly in the matched user's row.

    OUTPUT:
    Return ONLY a valid JSON object. Do not include markdown.
    The object must use this schema:
    {{
      "matched_row_name": "Name exactly as shown on the schedule",
      "row_weekly_hours": "Weekly-hours total if visible",
      "schedule_week_start": "YYYY-MM-DD (first date of the schedule week)",
      "schedule_week_end": "YYYY-MM-DD (last date of the schedule week)",
      "row_cells": {{
        "Full printed header (e.g. Mon Jun 8)": "raw visible cell text or empty string"
      }},
      "shifts": [
        {{
          "item_type": "reminder",
          "title": "Work shift - role or position",
          "description": "Matched schedule row: NAME. Include role/location if visible.",
          "due_date": "YYYY-MM-DDTHH:MM:SS",
          "end_date": "YYYY-MM-DDTHH:MM:SS",
          "assignee": "me",
          "assigner": "Uploaded work schedule",
          "priority": "normal",
          "is_all_day": false,
          "confidence": 0-100,
          "matched_row_name": "Name exactly as shown on the schedule",
          "row_weekly_hours": "Weekly-hours total if visible",
          "raw_cell_text": "Exact text read from the matched row's day cell",
          "reasoning": "Brief reason this row matched the user"
        }}
      ]
    }}

    A valid result must have shift durations that roughly match the row's Wkly Hrs total when one is visible.
    For example, if the row shows Wkly Hrs 34.5, the extracted shifts should add up to approximately 34.5 hours.
    """

def parse_schedule_image_response(response_text: str, current_time: Optional[str] = None) -> List[Dict[str, Any]]:
    extracted = extract_json(response_text)
    if len(extracted) == 1 and isinstance(extracted[0], dict) and "shifts" in extracted[0]:
        container = extracted[0]
        cell_tasks = row_cells_to_tasks(container, current_time)
        if cell_tasks:
            return cell_tasks
        raw_tasks = container.get("shifts", [])
        if isinstance(raw_tasks, dict):
            container["row_cells"] = raw_tasks
            cell_tasks = row_cells_to_tasks(container, current_time)
            if cell_tasks:
                return cell_tasks
            raw_tasks = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                continue
            raw_task.setdefault("matched_row_name", container.get("matched_row_name"))
            raw_task.setdefault("row_weekly_hours", container.get("row_weekly_hours"))
    elif len(extracted) == 1 and isinstance(extracted[0], dict) and "row_cells" in extracted[0]:
        cell_tasks = row_cells_to_tasks(extracted[0], current_time)
        if cell_tasks:
            return cell_tasks
        raw_tasks = []
    else:
        raw_tasks = extracted

    tasks = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            continue
        valid = validate_task(raw_task)
        valid["item_type"] = "reminder"
        valid["priority"] = "normal"
        valid["is_all_day"] = False
        valid["assigner"] = raw_task.get("assigner") or "Uploaded work schedule"
        valid["confidence"] = int(raw_task.get("confidence", valid.get("confidence", 80)))
        valid["matched_row_name"] = raw_task.get("matched_row_name")
        valid["row_weekly_hours"] = raw_task.get("row_weekly_hours")
        valid["raw_cell_text"] = raw_task.get("raw_cell_text")
        tasks.append(valid)
    return tasks

def extract_work_schedule_from_image(
    image_bytes: bytes,
    mime_type: str,
    target_names: List[str],
    current_time: Optional[str] = None
) -> list:
    if not client:
        raise RuntimeError("Image schedule extraction requires an OpenAI API key.")

    names = [name for name in target_names if name and name.strip()]
    if not names:
        raise RuntimeError("Clerk needs a profile name or username before extracting a personal work schedule.")

    now_iso = current_time if current_time else datetime.now(timezone.utc).isoformat()
    grid_tasks = extract_work_schedule_from_grid_image(image_bytes, names, current_time)
    if grid_tasks:
        return [format_for_frontend(task) for task in grid_tasks]

    optimized_bytes, optimized_mime_type = optimize_schedule_image(image_bytes, mime_type)
    encoded_image = base64.b64encode(optimized_bytes).decode("ascii")

    for retry in (False, True):
        prompt = build_schedule_image_prompt(names, now_iso, retry=retry)
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{optimized_mime_type};base64,{encoded_image}", "detail": "high"}}
                ]
            }],
            temperature=0,
            max_completion_tokens=SCHEDULE_MAX_OUTPUT_TOKENS
        )

        tasks = parse_schedule_image_response(response.choices[0].message.content, current_time)
        valid_tasks = validate_schedule_extraction(tasks, names)
        if valid_tasks:
            return [format_for_frontend(task) for task in valid_tasks]

    return []


def extract_work_schedule_from_text(
    schedule_text: str,
    target_names: List[str],
    current_time: Optional[str] = None
) -> list:
    if not client:
        return []

    names = [name for name in target_names if name and name.strip()]
    if not names:
        return []

    now_iso = current_time if current_time else datetime.now(timezone.utc).isoformat()
    prompt = f"""
    You are Clerk's work-schedule extraction engine.
    Current Local Timestamp: {now_iso}

    TASK:
    Read this weekly employee schedule text. Extract ONLY the work shifts for the logged-in user.
    Match the user by comparing schedule row names against these possible names:
    {json.dumps(names)}

    RULES:
    - Names may appear as "Last, First" while the profile may be "First Last".
    - Use the best matching employee row only.
    - If no row confidently matches the user, return [].
    - Use week/date headers to produce exact dated start and end timestamps.

    OUTPUT:
    Return ONLY a valid JSON list of Clerk task objects using this schema:
    {{
      "item_type": "reminder",
      "title": "Work shift - role or position",
      "description": "Matched schedule row: NAME. Include role/location if visible.",
      "due_date": "YYYY-MM-DDTHH:MM:SS",
      "end_date": "YYYY-MM-DDTHH:MM:SS",
      "assignee": "me",
      "assigner": "Uploaded work schedule",
      "priority": "normal",
      "is_all_day": false,
      "confidence": 0-100,
      "reasoning": "Brief reason this row matched the user"
    }}

    SCHEDULE TEXT:
    {schedule_text[:MAX_AI_INPUT_CHARS]}
    """

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_completion_tokens=SCHEDULE_MAX_OUTPUT_TOKENS
    )

    tasks = []
    for raw_task in extract_json(response.choices[0].message.content):
        valid = validate_task(raw_task)
        valid["item_type"] = "reminder"
        valid["priority"] = "normal"
        valid["is_all_day"] = False
        valid["assigner"] = raw_task.get("assigner") or "Uploaded work schedule"
        valid["confidence"] = int(raw_task.get("confidence", valid.get("confidence", 80)))
        tasks.append(format_for_frontend(valid))
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
        chunks = split_text_for_ai(text)
        if len(chunks) == 1:
            raw_tasks.extend(extract_json_from_chunk(chunks[0], now_iso))
        else:
            worker_count = max(1, min(MAX_AI_WORKERS, len(chunks)))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(extract_json_from_chunk, chunk, now_iso) for chunk in chunks]
                for future in as_completed(futures):
                    raw_tasks.extend(future.result())
        
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
            # Strip 'Z' or offsets to treat as "wall time" (local naive datetime).
            clean_date = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', task["due_date"])
            due_dt = datetime.fromisoformat(clean_date)
        except (ValueError, AttributeError):
            pass
            
    task["due"] = due_dt.strftime("%m/%d/%Y") if due_dt else "No due date"
    task["time"] = "All Day" if task.get("is_all_day") else (due_dt.strftime("%I:%M %p") if due_dt else "No time")
    return task
