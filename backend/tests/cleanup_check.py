"""Scratch-DB verification for the Google-sync lookback cleanup.

Run with DATABASE_URL pointed at a throwaway SQLite file:
    $env:DATABASE_URL = "sqlite:///C:/temp/clerk_cleanup_test.db"; python tests/cleanup_check.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from scripts.init_db import SessionLocal, Task, RawInput, User, ensure_database_schema
from app.main import cleanup_stale_synced_tasks, classroom_item_is_too_old, GOOGLE_SYNC_PAST_DAYS

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
print("ALL CLEANUP CHECKS PASSED")
