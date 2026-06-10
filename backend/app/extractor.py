# --- IMPORTS AND CLIENT SETUP ---
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

# Build the OpenAI client once at startup using environment variables.
# The client is set to None if no API key is found, so callers can check
# `if not client` to decide whether to fall back to local (regex) extraction.
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(
    api_key=api_key,
    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45")),
    max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "1"))
) if api_key else None

# --- CONFIGURATION CONSTANTS ---
# These values control how much text is sent to the AI and how images are processed.
# Reading them from environment variables lets you tune performance without changing code.
MAX_AI_INPUT_CHARS = int(os.getenv("EXTRACTOR_MAX_AI_INPUT_CHARS", "18000"))
MAX_AI_CHUNKS = int(os.getenv("EXTRACTOR_MAX_AI_CHUNKS", "3"))
MAX_AI_WORKERS = int(os.getenv("EXTRACTOR_MAX_AI_WORKERS", "3"))
SCHEDULE_IMAGE_MAX_SIDE = int(os.getenv("SCHEDULE_IMAGE_MAX_SIDE", "2048"))
SCHEDULE_IMAGE_QUALITY = int(os.getenv("SCHEDULE_IMAGE_QUALITY", "86"))
SCHEDULE_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_SCHEDULE_MAX_OUTPUT_TOKENS", "1800"))
# Overlap ensures that a task spanning a chunk boundary is not lost when text is split.
CHUNK_OVERLAP_CHARS = 500

# --- REGEX PATTERNS AND WORD LISTS ---
# These compiled patterns are defined once at module level so they are not
# recompiled on every function call — a significant speed improvement when
# processing many lines of text.

# Words that strongly suggest a sentence contains an actionable task.
ACTION_WORDS = {
    "add", "answer", "attend", "bring", "buy", "call", "check", "compile",
    "complete", "create", "do", "draft", "email", "finish", "fix", "implement",
    "make", "meet", "organize", "prepare", "present", "read", "record", "remind",
    "review", "schedule", "send", "study", "submit", "turn in", "update",
    "upload", "write"
}

# Matches any line that looks task-related (action verbs, assignment words,
# or date references) so the compactor can keep it and skip everything else.
TASK_HINT_RE = re.compile(
    r'\b('
    r'add|answer|attend|bring|buy|call|check|compile|complete|create|do|draft|email|finish|fix|'
    r'implement|make|meet|organize|prepare|present|read|record|remind|review|schedule|send|study|submit|'
    r'turn\s+in|update|upload|write|assignment|homework|project|quiz|test|exam|essay|'
    r'presentation|deadline|due|urgent|asap|today|tomorrow|next\s+\w+|monday|tuesday|'
    r'wednesday|thursday|friday|saturday|sunday|jan(?:uary)?|feb(?:ruary)?|'
    r'mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|'
    r'oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}[/-]\d{1,2}'
    r')\b',
    re.IGNORECASE
)

# Matches lines that are purely structural (page numbers, headings, etc.)
# so they can be dropped before sending text to the AI.
NOISE_LINE_RE = re.compile(
    r'^\s*(?:page\s+\d+|\d+|copyright|table of contents|references)\s*$',
    re.IGNORECASE
)

# A tighter set of action verbs used in confidence scoring — these are more
# reliable signals of an actionable task than the broader ACTION_WORDS set.
STRONG_ACTION_RE = re.compile(
    r'\b(submit|finish|complete|turn\s+in|write|create|prepare|review|send|'
    r'schedule|call|email|buy|bring|read|study|fix|implement|make|upload|'
    r'attend|present|record|organize|compile)\b',
    re.IGNORECASE
)

# Used in confidence scoring to detect explicit deadline language.
DEADLINE_RE = re.compile(
    r'\b(due|deadline|by|before|no later than|urgent|asap|immediately|critical)\b',
    re.IGNORECASE
)
# Hedging words lower confidence because they suggest the item may not be required.
AMBIGUITY_RE = re.compile(
    r'\b(maybe|might|possibly|probably|optional|if you can|when you can|sometime|'
    r'eventually|consider|think about|maybe later|tentative|whenever|flexible|'
    r'when possible|if possible|as needed|try to)\b',
    re.IGNORECASE
)
# Matches clock times like "3pm", "10:30 AM", or "14:00" to detect precise scheduling.
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

# --- DATE AND TIME LOOKUP TABLES ---
# Mapping month name strings (including abbreviations) to their numeric value
# so that date strings like "Jan 15" or "January 15" can be parsed consistently.
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12
}

# Python's datetime.weekday() uses Mon=0 … Sun=6, matching this table.
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6
}

# --- AI PROMPT BUILDING ---
def build_prompt(user_input: str, current_time: str) -> str:
    """Build the system prompt sent to the AI for general task extraction.

    Injecting the current timestamp tells the AI how to resolve relative
    dates like 'tomorrow' or 'next Friday' into concrete ISO 8601 values.
    """
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
    inside the raw document text. Checks ISO format, numeric M/D,
    and month-name + day formats to reduce false negatives.
    """
    if not extracted_date:
        return True

    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', extracted_date)
    if not date_match:
        return True

    target = date_match.group(1)
    if target in raw_text or target.replace('-', '/') in raw_text:
        return True

    try:
        dt = datetime.fromisoformat(extracted_date.replace('Z', ''))
        lowered = raw_text.lower()
        day_str = str(dt.day)
        # Check "May 12", "May 12th", "may 12" etc.
        for month_variant in (dt.strftime('%b').lower(), dt.strftime('%B').lower()):
            if re.search(rf'\b{month_variant}\s+{day_str}(?:st|nd|rd|th)?\b', lowered):
                return True
        # Check numeric "5/12" or "5-12" (M/D without year)
        if re.search(rf'\b{dt.month}[/-]{day_str}(?:[/-]\d{{2,4}})?\b', raw_text):
            return True
        # Check weekday name ("Saturday", "Sat")
        if dt.strftime('%A').lower() in lowered or dt.strftime('%a').lower() in lowered:
            return True
    except (ValueError, AttributeError):
        pass
    return False

# --- JSON PARSING HELPERS ---
def extract_json(text: str) -> list:
    """Parse the AI's response text into a Python list of task dicts.

    The AI sometimes wraps its output in markdown code fences (```json ... ```)
    even when told not to, so those are stripped before parsing. As a fallback,
    a regex scans for the first valid JSON array or object in the text.
    """
    trimmed = text.replace('```json', '').replace('```', '').strip()
    try:
        data = json.loads(trimmed)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        # Try to find a JSON array anywhere in the string (e.g. after extra prose).
        match = re.search(r'\[[\s\S]*\]', trimmed)
        if match:
            return json.loads(match.group(0))
        obj_match = re.search(r'\{[\s\S]*\}', trimmed)
        if obj_match:
            return [json.loads(obj_match.group(0))]
        raise ValueError("Failed to parse AI response as JSON")

# --- SCORING AND DATETIME UTILITIES ---
def clamp_score(score: float) -> int:
    """Ensure a confidence score stays in the valid 0–100 integer range."""
    return int(max(0, min(100, round(score))))

def parse_task_datetime(value: Optional[str]) -> Optional[datetime]:
    """Convert an ISO 8601 date string into a naive datetime object.

    Timezone suffixes (Z or +HH:MM) are stripped so all dates are treated as
    wall-clock 'local' time, avoiding accidental timezone conversion bugs.
    """
    if not value:
        return None
    try:
        cleaned = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', str(value))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None

def title_terms(title: Optional[str]) -> List[str]:
    """Extract meaningful words from a task title for source-text matching.

    Common stop words are removed because they appear everywhere and would
    produce false-positive matches when checking whether a task is grounded
    in the original document.
    """
    terms = re.findall(r'[a-z0-9]{3,}', str(title or "").lower())
    stop_words = {"the", "and", "for", "with", "task", "new", "due", "assignment"}
    return [term for term in terms if term not in stop_words]

# --- EVIDENCE EXTRACTION ---
def evidence_window(source_text: str, task: Dict[str, Any], radius: int = 500) -> str:
    """Return a short slice of the source text surrounding where the task was found.

    Limiting the window to ±500 characters around the matching term keeps the
    context relevant for scoring without re-scanning the entire document.
    """
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
    """Pull just the YYYY-MM-DD date portion out of a full ISO timestamp string."""
    if not due_date:
        return ""
    match = re.search(r'\d{4}-\d{2}-\d{2}', str(due_date))
    return match.group(0) if match else ""

# --- TEXT COMPACTION AND CHUNKING ---
def compact_text_for_extraction(text: str, max_chars: int = MAX_AI_INPUT_CHARS) -> str:
    """Shrink a large document down to its task-relevant lines before sending to the AI.

    Scanning line-by-line and keeping only lines that match TASK_HINT_RE avoids
    wasting AI tokens on boilerplate, saving cost and reducing hallucination risk.
    """
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

        # Include one line of context above and below the matched line so the
        # AI has enough surrounding text to understand the task's meaning.
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
    """Compact a document and split it into overlapping chunks for parallel AI calls.

    Each chunk overlaps the previous one by CHUNK_OVERLAP_CHARS characters so that
    a task sentence that falls on a boundary is fully captured in at least one chunk.
    """
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
        # Step forward by (chunk_size - overlap) so the next chunk revisits the tail
        # of this one, preventing tasks from being silently cut off at chunk edges.
        start = max(end - CHUNK_OVERLAP_CHARS, start + 1)
    return chunks

# --- DATE AND TIME PARSING ---
def parse_current_time(current_time: Optional[str]) -> datetime:
    """Parse the client-provided local timestamp into a naive datetime.

    The "(Local Time)" annotation the frontend appends is stripped, and the
    timezone info is removed so all subsequent date arithmetic stays timezone-naive
    and avoids accidental UTC offsets when resolving 'today' or 'tomorrow'.
    """
    if not current_time:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    cleaned = current_time.replace("(Local Time)", "").strip()
    # Python's fromisoformat needs "+00:00" format, not the bare "Z" suffix.
    cleaned = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None)

def parse_time_fragment(text: str):
    """Extract an hour and minute from a string like '3pm' or 'at 10:30 AM'.

    Returns (hour, minute, is_all_day). If no clock time is found, defaults to
    noon and marks the task as all-day so callers know the time was not explicit.
    The 12 PM / 12 AM edge case (noon vs midnight) requires special handling
    because standard 12-hour clock rules do not follow simple +12/-12 arithmetic.
    """
    match = re.search(r'\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text, re.IGNORECASE)
    if not match:
        return 12, 0, True

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3).lower()
    # "12 PM" is noon (no change), but "1 PM" through "11 PM" need +12.
    if suffix == "pm" and hour != 12:
        hour += 12
    # "12 AM" is midnight (hour=0), but "1 AM" through "11 AM" are already correct.
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute, False

def parse_due_date(text: str, now: datetime):
    """Convert natural-language date references in text into an ISO timestamp.

    Handles relative terms ('today', 'tomorrow', 'next Monday'), numeric dates
    ('05/12'), and month-name dates ('May 12'). Returns a tuple of
    (due_date_str, end_date_str, is_all_day).
    """
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
            # If the day has already passed this week (or "next X" was explicit),
            # add 7 to jump to the following week's occurrence.
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
    """Strip date/time tails and filler phrases from a raw sentence to produce a clean title.

    For example, "remind me to call Mom by Friday at 3pm" becomes "Call Mom".
    "by/due" are cut unconditionally; "on/at" only when followed by a date/time token
    so "meet at headquarters" is preserved while "at 3pm" is removed.
    """
    # "by Friday", "due Jan 5" — always a date tail
    title = re.sub(r'\b(?:by|due)\s+\S+.*$', '', text, flags=re.IGNORECASE).strip()
    # "at 3pm", "on Monday", "on Jan 5" — only cut when the next word is a time/date token
    title = re.sub(
        r'\b(?:on|at)\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|'
        r'(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
        r'today|tomorrow|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)).*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    title = re.sub(
        r'^(please\s+|remind me to\s+|remind me\s+|i need to\s+|need to\s+|can you\s+)',
        '', title, flags=re.IGNORECASE
    )
    return title[:1].upper() + title[1:] if title else "New Task"

# --- LOCAL (REGEX-BASED) EXTRACTION ---
def local_nlp_extract_tasks(text: str, current_time: Optional[str] = None) -> List[Dict[str, Any]]:
    """Extract tasks using only regex — no AI API call required.

    This is the offline fallback used when the OpenAI client is unavailable
    or when the AI call fails. It is less accurate than the AI path but
    guarantees that the app still works without an API key.
    """
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

# --- AI CHUNK EXTRACTION ---
def extract_json_from_chunk(chunk: str, now_iso: str) -> list:
    """Send one text chunk to the AI and return the parsed list of task dicts.

    Temperature=0 is used to make the output deterministic and reduce creative
    hallucination — the AI should read the text, not invent tasks.
    """
    prompt = build_prompt(chunk, now_iso)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_completion_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500"))
    )
    return extract_json(response.choices[0].message.content)

# --- SCHEDULE IMAGE PREPROCESSING ---
def optimize_schedule_image(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Resize and compress a schedule image before sending it to the AI vision API.

    Keeping images within SCHEDULE_IMAGE_MAX_SIDE pixels reduces API cost and
    latency. EXIF transpose corrects phone photos that are rotated by metadata
    without actually rotating the pixels.
    """
    try:
        from PIL import Image, ImageOps
        from io import BytesIO
    except Exception:
        return image_bytes, mime_type

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            # Fix rotation from camera EXIF data before resizing.
            image = ImageOps.exif_transpose(image)
            image.thumbnail((SCHEDULE_IMAGE_MAX_SIDE, SCHEDULE_IMAGE_MAX_SIDE))
            # JPEG requires RGB or greyscale; convert exotic modes (RGBA, P, etc.).
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            output = BytesIO()
            image.save(output, format="JPEG", quality=SCHEDULE_IMAGE_QUALITY, optimize=True)
            return output.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, mime_type

# --- SCHEDULE NAME MATCHING ---
def normalize_schedule_name(value: Optional[str]) -> str:
    """Convert a name to a lowercase, punctuation-stripped form for comparison.

    The "Last, First" → "First Last" swap handles schedules that store names
    in reverse order compared to the user's profile display name.
    """
    text = re.sub(r'[^a-z\s,]', ' ', str(value or "").lower())
    text = re.sub(r'\s+', ' ', text).strip()
    if "," in text:
        last, first = [part.strip() for part in text.split(",", 1)]
        text = f"{first} {last}".strip()
    return text

def schedule_names_match(row_name: Optional[str], target_names: List[str]) -> bool:
    """Check whether a schedule row name belongs to the target user.

    A blank row name is treated as a match (True) so that schedules without
    a name column are not silently rejected. Token overlap of at least 2 words
    (or all words if the name is one word) tolerates nicknames and initials.
    """
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
        # Require at least 2 shared tokens (or all tokens if the name is short)
        # to avoid matching on common single-word coincidences like "Lee".
        if len(row_tokens & target_tokens) >= min(2, len(target_tokens)):
            return True
    return False

# --- SCHEDULE SHIFT CALCULATIONS ---
def parse_schedule_weekly_hours(value: Any) -> Optional[float]:
    """Extract a floating-point hours value from a string like '34.5 hrs' or '40'."""
    match = re.search(r'\d+(?:\.\d+)?', str(value or ""))
    return float(match.group(0)) if match else None

def parse_schedule_datetime(value: Any) -> Optional[datetime]:
    """Convert a schedule timestamp string to a naive datetime, ignoring timezone."""
    if not value:
        return None
    try:
        cleaned = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', str(value))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None

def schedule_shift_hours(task: Dict[str, Any]) -> float:
    """Calculate the duration of a single shift in hours from its due_date and end_date.

    If end < start (e.g., a shift crosses midnight), one day is added to end
    before computing the difference to avoid a negative duration.
    """
    start = parse_schedule_datetime(task.get("due_date"))
    end = parse_schedule_datetime(task.get("end_date"))
    if not start or not end:
        return 0
    if end < start:
        end += timedelta(days=1)
    return max(0, (end - start).total_seconds() / 3600)

def extracted_schedule_total_hours(tasks: List[Dict[str, Any]]) -> float:
    """Sum the hours of all shifts in a list, rounded to two decimal places."""
    return round(sum(schedule_shift_hours(task) for task in tasks), 2)

# --- SCHEDULE DAY LABEL PARSING ---
def parse_schedule_day_label(label: str, current_time: Optional[str]) -> Optional[datetime]:
    """Convert a schedule column header (e.g. 'Mon', 'Jun 8') to an absolute date.

    For weekday-only labels, the function finds the corresponding day in the
    Sun–Sat calendar week that contains `now`, rather than searching ±7 days,
    which would be wrong when the schedule week does not match the current week.
    """
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
    """Convert 12-hour clock components into a 24-hour (hour, minute) pair.

    Applying the same noon/midnight edge-case logic as parse_time_fragment
    ensures consistent AM/PM conversion across all time-parsing code paths.
    """
    hour = int(hour_text)
    minute = int(minute_text or 0)
    suffix = suffix.lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute

# --- SCHEDULE GRID CELL CONVERSION ---
def row_cell_to_task(
    day_label: str,
    cell_text: Any,
    matched_row_name: Optional[str],
    row_weekly_hours: Optional[Any],
    current_time: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Parse a single schedule grid cell (one day's shift text) into a task dict.

    Returns None if the cell is blank or explicitly marks a day off, so callers
    can safely skip it without extra conditional logic.
    """
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
    """Convert a row_cells dict (or list) from the AI response into a list of shift tasks.

    The AI sometimes returns row_cells as a list indexed Sun–Sat, so we normalize
    it to a dict before iterating to keep the rest of the logic consistent.
    """
    row_cells = container.get("row_cells")
    if isinstance(row_cells, list):
        # Map positional list → {"Sun": ..., "Mon": ..., ...}
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

# --- SCHEDULE VALIDATION ---
def validate_schedule_extraction(tasks: List[Dict[str, Any]], target_names: List[str]) -> List[Dict[str, Any]]:
    """Sanity-check a list of extracted schedule tasks and discard implausible results.

    Multiple heuristics are applied — name matching, total-hours tolerance, and
    a 16-hour shift cap — because AI vision can misread rows and produce confident
    but wrong output that needs to be caught before it reaches the user.
    """
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

# --- SCHEDULE IMAGE ENCODING ---
def encode_schedule_image(image, max_side: int = 2048) -> str:
    """Sharpen, resize, and base64-encode a PIL image for the OpenAI vision API.

    Contrast and sharpness are boosted before encoding because schedule grids
    have thin lines and small text that vision models struggle to read at normal
    image quality. Very short images (< 180px tall) are scaled up 3× first.
    """
    from PIL import ImageEnhance
    from io import BytesIO

    image = ImageEnhance.Contrast(image).enhance(1.35)
    image = ImageEnhance.Sharpness(image).enhance(1.55)
    # Upscale tiny crops (e.g. individual cells) so text is legible to the AI.
    if image.height < 180:
        image = image.resize((image.width * 3, image.height * 3))
    image.thumbnail((max_side, max_side))
    output = BytesIO()
    image.save(output, format="JPEG", quality=max(SCHEDULE_IMAGE_QUALITY, 92), optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")

# --- GRID DETECTION ---
def detect_schedule_grid(image) -> tuple[List[int], List[tuple[int, int]]]:
    """Detect horizontal row lines and vertical day-column lines in a schedule image.

    Returns (day_lines, row_bounds) where day_lines is an 8-element list of x
    coordinates bounding the 7 day columns, and row_bounds is a list of (top, bottom)
    y-pixel pairs for each employee row. Returns ([], []) if the image does not
    look like a grid (fewer than 4 horizontal lines detected).

    The algorithm works by scanning rows/columns of pixels for 'dark' density
    above a threshold — schedule grid lines appear as many consecutive dark pixels.
    Nearby dark pixels are clustered together and averaged to find the true line center.
    """
    from PIL import ImageOps

    gray = ImageOps.grayscale(image)
    width, height = gray.size
    pixels = gray.load()
    # Ignore the outer 5% of the image to avoid border artifacts.
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

    # Select the best sequence of 8 evenly-spaced column lines from the candidates.
    # A schedule has 7 day columns (8 boundary lines). We search for the 8-line
    # sequence with the lowest variance in gap widths — equal gaps = a real grid.
    # An iterative stack is used instead of recursion to avoid Python's recursion limit
    # on images with many candidate column lines.
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
                # Penalise sequences that don't span the expected image width,
                # since a real 7-column grid should reach from ~25% to ~94% of the image.
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
                    break  # Gaps beyond 190px are too large to be adjacent day columns.
                if 115 <= gap <= 190:
                    stack.append((sequence + [candidates[next_index]], next_index))
    day_lines = best if len(best) >= 8 else []

    row_bounds = [
        (horizontal_lines[index], horizontal_lines[index + 1])
        for index in range(len(horizontal_lines) - 1)
        if 34 <= horizontal_lines[index + 1] - horizontal_lines[index] <= 95
    ]
    return day_lines, row_bounds

# --- CONTACT SHEET BUILDING ---
def build_row_contact_sheet(image, day_lines: List[int], row_bounds: List[tuple[int, int]]):
    """Build a single composite image showing all name cells side-by-side for the AI.

    Sending one image with numbered row labels is far cheaper (fewer API calls)
    than asking the vision model to locate a name directly in the full schedule
    image, which may have dozens of rows and be hard to read at small size.
    """
    from PIL import Image, ImageDraw

    if not day_lines or not row_bounds:
        return None

    # The name column sits to the left of the first day-column boundary (day_lines[0]).
    # Estimate its width as ~1.95× the width of one day column.
    name_left = max(0, day_lines[0] - int((day_lines[1] - day_lines[0]) * 1.95))
    name_right = day_lines[0]
    cell_width = name_right - name_left
    cell_height = 72
    label_width = 70
    sheet = Image.new("RGB", (label_width + cell_width * 2, cell_height * len(row_bounds)), "white")
    draw = ImageDraw.Draw(sheet)

    for index, (top, bottom) in enumerate(row_bounds):
        crop = image.crop((name_left, max(0, top - 3), name_right, min(image.height, bottom + 3)))
        # Scale up each name crop so the AI can read small text more reliably.
        crop = crop.resize((cell_width * 2, cell_height))
        y = index * cell_height
        draw.text((8, y + 24), f"{index}", fill="black")
        sheet.paste(crop, (label_width, y))
    return sheet

# --- AI-ASSISTED ROW LOCATION ---
def locate_schedule_row_from_image(image, day_lines: List[int], row_bounds: List[tuple[int, int]], target_names: List[str]) -> Optional[Dict[str, Any]]:
    """Use the AI vision model to identify which row index belongs to the target user.

    Sends the contact sheet (name cells only) to the AI so it reads just the
    names — not the whole schedule — making the match faster and more accurate.
    Returns a dict with row_index, matched_row_name, and row_weekly_hours.
    """
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
        model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
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

# --- PER-ROW SCHEDULE EXTRACTION ---
def extract_schedule_from_detected_row(image, row_info: Dict[str, Any], day_lines: List[int], row_bounds: List[tuple[int, int]], current_time: Optional[str]) -> List[Dict[str, Any]]:
    """Crop each day cell from the identified employee row and send them to the AI.

    Sending individual cell crops alongside the date header strip lets the model
    read each shift independently, reducing the chance of it copying a time from
    a neighboring row or the wrong column.
    """
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
        model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
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

# --- FULL GRID SCHEDULE PIPELINE ---
def extract_work_schedule_from_grid_image(image_bytes: bytes, target_names: List[str], current_time: Optional[str]) -> List[Dict[str, Any]]:
    """Orchestrate the full pixel-based grid extraction pipeline.

    This is the preferred path for schedule images because it is more accurate
    than sending the whole image at once — it surgically isolates the user's row
    before asking the AI to read it. Returns [] if grid detection fails, signalling
    the caller to fall back to the full-image AI prompt approach.
    """
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

# --- SCHEDULE IMAGE PROMPT BUILDING ---
def build_schedule_image_prompt(target_names: List[str], now_iso: str, retry: bool = False) -> str:
    """Build the prompt for the full-image schedule extraction fallback.

    When retry=True, extra instructions are injected that tell the AI to be
    stricter about row boundaries, because the first attempt was rejected by
    validate_schedule_extraction (likely due to row-bleed or hour mismatch).
    """
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

# --- SCHEDULE IMAGE RESPONSE PARSING ---
def parse_schedule_image_response(response_text: str, current_time: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse and normalise the AI's schedule response into a flat list of shift tasks.

    The AI may return the data in several shapes — a container object with a
    'shifts' list, a container with 'row_cells', or a bare list — so each case
    is handled explicitly before falling back to treating the response as a list.
    """
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

# --- PUBLIC SCHEDULE IMAGE EXTRACTION ENTRY POINT ---
def extract_work_schedule_from_image(
    image_bytes: bytes,
    mime_type: str,
    target_names: List[str],
    current_time: Optional[str] = None
) -> list:
    """Top-level function to extract work shifts from a schedule image.

    Tries the precise pixel-based grid pipeline first; if that returns nothing
    (grid not detected or row not found), falls back to sending the whole image
    to the AI with a descriptive prompt, retrying once with stricter instructions
    if the first result fails validation.
    """
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

    # Attempt extraction twice: first with standard instructions, then with
    # stricter retry instructions if the initial result fails validation.
    for retry in (False, True):
        prompt = build_schedule_image_prompt(names, now_iso, retry=retry)
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
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


# --- SCHEDULE TEXT EXTRACTION ---
def extract_work_schedule_from_text(
    schedule_text: str,
    target_names: List[str],
    current_time: Optional[str] = None
) -> list:
    """Extract work shifts from a plain-text schedule (e.g., a copied spreadsheet).

    Used when the schedule is provided as text rather than an image, so the AI
    reads structured rows and columns as characters instead of pixels.
    """
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

# --- MAIN TEXT EXTRACTION ENTRY POINT ---
def extract_task_from_text(text: str, current_time: Optional[str] = None) -> list:
    """Extract all tasks from a user message or uploaded document text.

    Uses the AI when available, splitting large documents into parallel chunks
    for speed. Falls back silently to local_nlp_extract_tasks if the AI client
    is absent or any exception occurs during extraction.
    """
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
            # Process multiple chunks in parallel using a thread pool so that
            # long documents don't take 3× as long as a single short message.
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

# --- CONFIDENCE SCORING ---
def adjust_confidence(
    user_input: str,
    task: Dict[str, Any],
    current_time: Optional[str] = None,
    date_verified: bool = True
) -> Dict[str, Any]:
    """Recalculate a task's confidence score using multiple evidence signals.

    The AI's own confidence value is blended with regex-based checks on action
    language, deadline words, title quality, and date proximity. This hybrid
    approach prevents the AI from being overconfident on vague or ambiguous input.
    The formula starts at 42 and adds/subtracts small amounts per signal so the
    final score stays within a realistic 0–100 range.
    """
    model_score = max(0, min(100, int(task.get("confidence", 70))))
    # Start at a baseline of 42 + a dampened fraction of the AI's own score.
    # Using 0.32 keeps the AI's contribution meaningful but not dominant.
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

        # A date that cannot be found anywhere in the source text is likely
        # hallucinated by the AI, so it earns a large penalty.
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

# --- TASK VALIDATION ---
def validate_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a raw AI task dict into the standard Clerk schema.

    Handles missing fields with safe defaults and collapses priority aliases
    (e.g. 'urgent' → 'high') so the rest of the codebase only sees 'low',
    'normal', or 'high'.
    """
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
        # Fall back through several field names the AI may use for the assigner.
        "assigner": task.get("assigner") or task.get("assigned_by") or task.get("teacher") or task.get("sender"),
        "is_all_day": bool(task.get("is_all_day", False)),
        "priority": p,
        "confidence": int(task.get("confidence", 70))
    }

# --- FRONTEND FORMATTING ---
def format_for_frontend(task: Dict[str, Any]) -> Dict[str, Any]:
    """Add human-readable 'due' and 'time' display fields to a task dict.

    The frontend expects these pre-formatted strings rather than computing them
    from the raw ISO timestamp, keeping all date formatting logic in one place.
    """
    due_dt = None
    if task.get("due_date"):
        try:
            # Strip timezone info to treat the timestamp as local wall-clock time,
            # so "9:00 AM" displays correctly regardless of server timezone.
            clean_date = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', task["due_date"])
            due_dt = datetime.fromisoformat(clean_date)
        except (ValueError, AttributeError):
            pass

    task["due"] = due_dt.strftime("%m/%d/%Y") if due_dt else "No due date"
    task["time"] = "All Day" if task.get("is_all_day") else (due_dt.strftime("%I:%M %p") if due_dt else "No time")
    return task
