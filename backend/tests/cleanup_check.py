"""Scratch-DB verification for the Google-sync lookback cleanup.

Run with DATABASE_URL pointed at a throwaway SQLite file:
    $env:DATABASE_URL = "sqlite:///C:/temp/clerk_cleanup_test.db"; python tests/cleanup_check.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from scripts.init_db import SessionLocal, Task, RawInput, User, ensure_database_schema
from app.main import (
    cleanup_stale_synced_tasks,
    cleanup_unwanted_calendar_imports,
    classroom_item_is_too_old,
    is_holiday_calendar_event,
    is_noise_calendar,
    GOOGLE_SYNC_PAST_DAYS,
)

ensure_database_schema()
db = SessionLocal()
user = User(username=f"cleanup_test_{datetime.now().timestamp()}", password_hash="x")
db.add(user)
db.flush()


def add(source_type, title, due, status="pending"):
    raw = RawInput(content="t", source_type=source_type, source_id=f"{user.user_id}:{source_type}:{title}")
    db.add(raw)
    db.flush()
    t = Task(owner_id=user.user_id, raw_id=raw.raw_id, title=title, due_date=due, status=status)
    db.add(t)
    db.flush()
    return t


old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%dT12:00:00")
recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT12:00:00")
future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT12:00:00")

t1 = add("classroom: abc", "Old classroom assignment", old)        # cleaned
t2 = add("gmail: xyz", "Old email task", old)                      # cleaned
t3 = add("classroom: def", "Recent classroom assignment", recent)  # kept
t4 = add("calendar: ghi", "Future event", future)                  # kept
t5 = add("text:tag:0", "Old personal note task", old)              # kept (user-created)
t6 = add("classroom: jkl", "Old but completed", old, status="completed")  # untouched
db.commit()

removed = cleanup_stale_synced_tasks(db, user.user_id)
states = {t.title: db.query(Task).get(t.task_id).status for t in (t1, t2, t3, t4, t5, t6)}
print("window days:", GOOGLE_SYNC_PAST_DAYS)
print("removed:", removed)
for k, v in states.items():
    print(f"  {k}: {v}")

assert removed == 2
assert states["Old classroom assignment"] == "deleted"
assert states["Old email task"] == "deleted"
assert states["Recent classroom assignment"] == "pending"
assert states["Future event"] == "pending"
assert states["Old personal note task"] == "pending"
assert states["Old but completed"] == "completed"

assert classroom_item_is_too_old({"dueDate": {"year": 2026, "month": 1, "day": 24}}) is True
assert classroom_item_is_too_old({"dueDate": {"year": 2026, "month": 6, "day": 20}}) is False
assert classroom_item_is_too_old({"creationTime": "2026-01-05T10:00:00Z"}) is True
assert classroom_item_is_too_old({"creationTime": "2026-06-10T10:00:00Z"}) is False
# Old post with a future deadline is still relevant.
assert classroom_item_is_too_old({"creationTime": "2026-01-05T10:00:00Z", "dueDate": {"year": 2026, "month": 6, "day": 25}}) is False

# Holiday observance / far-future calendar cleanup
far_future = (datetime.now() + timedelta(days=1400)).strftime("%Y-%m-%dT12:00:00")
near_future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT12:00:00")

def add_event(title, due, description, assignee="Google Calendar", source_type=None):
    raw_source_type = source_type or f"calendar: {title}"
    raw = RawInput(content="t", source_type=raw_source_type, source_id=f"{user.user_id}:{raw_source_type}:{title}")
    db.add(raw)
    db.flush()
    t = Task(owner_id=user.user_id, raw_id=raw.raw_id, title=title, due_date=due,
             description=description, item_type="event", assignee=assignee, status="pending")
    db.add(t)
    db.flush()
    return t

h1 = add_event("Tax Day", near_future, "Observance To hide observances, go to Google Calendar Settings > Holidays in United States")
h2 = add_event("Bank Holiday", near_future, "Public holiday")
e1 = add_event("Team offsite", near_future, "Quarterly planning")          # kept
e2 = add_event("Graduation 2030", far_future, "Family event")              # hard-deleted (re-imports later)
e2_raw_id = e2.raw_id
e3 = add_event("Company day off", near_future, "Public holiday", assignee="me", source_type="text:manual")
db.commit()

removed = cleanup_unwanted_calendar_imports(db, user.user_id)
print("calendar noise removed:", removed)
assert removed == 3
assert db.get(Task, h1.task_id).status == "deleted"          # observance soft-deleted
assert db.get(Task, h2.task_id).status == "deleted"          # public holiday soft-deleted
assert db.get(Task, e1.task_id).status == "pending"          # real upcoming event kept
assert db.get(Task, e2.task_id) is None                      # far-future hard-deleted
assert db.get(RawInput, e2_raw_id) is None                   # marker removed so it re-imports in window
assert db.get(Task, e3.task_id).status == "pending"          # personal/manual event kept

# Calendar discovery filter
assert is_noise_calendar("en.usa#holiday@group.v.calendar.google.com", "Holidays in United States") is True
assert is_noise_calendar("en.usa%23holiday@group.v.calendar.google.com", "Holidays in United States") is True
assert is_noise_calendar("abc#weeknum@group.v.calendar.google.com", "Week Numbers") is True
assert is_noise_calendar("primary", "John's Calendar") is False
assert is_noise_calendar("addressbook#contacts@group.v.calendar.google.com", "Birthdays") is False

# Event-level holiday guard for renamed/shared calendars
assert is_holiday_calendar_event(
    {"summary": "Flag Day", "description": "Observance To hide observances, go to Google Calendar Settings > Holidays in United States"},
    "primary",
    "Shared reminders",
) is True
assert is_holiday_calendar_event(
    {"summary": "Father's Day brunch", "description": "Family lunch"},
    "primary",
    "John's Calendar",
) is False

print("ALL CLEANUP CHECKS PASSED")
