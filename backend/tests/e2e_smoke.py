"""End-to-end smoke test for the Clerk API.

Boots nothing itself — expects a running server (see run instructions below).
Exercises auth, ownership checks, text ingestion, dedup, and the insights summary.

    # Terminal 1 (from backend/):
    #   $env:DATABASE_URL="sqlite:///C:/temp/clerk_e2e.db"; $env:OPENAI_API_KEY=""
    #   python -m uvicorn app.main:app --port 8123
    # Terminal 2:
    #   python tests/e2e_smoke.py http://127.0.0.1:8123
"""
import sys
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
PASSWORD = "Sm0ke-Test!Pass99"

failures = []

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)

# 1. Register a fresh user
import uuid
username = f"smoke_{uuid.uuid4().hex[:8]}"
r = requests.post(f"{BASE}/register", json={"username": username, "password": PASSWORD})
check("register returns 200", r.status_code == 200, r.text)
token = r.json().get("token")
check("register issues a session token", bool(token))
user_id = r.json()["user_id"]
auth = {"Authorization": f"Bearer {token}"}

# 2. Unauthenticated access must be rejected
r = requests.get(f"{BASE}/tasks", params={"user_id": user_id})
check("GET /tasks without token -> 401", r.status_code == 401, f"got {r.status_code}")
r = requests.post(f"{BASE}/transcribe", files={"file": ("a.mp3", b"xx", "audio/mpeg")})
check("POST /transcribe without token -> 401", r.status_code == 401, f"got {r.status_code}")
r = requests.delete(f"{BASE}/users/{user_id}")
check("DELETE /users without token -> 401", r.status_code == 401, f"got {r.status_code}")

# 3. Login flow
r = requests.post(f"{BASE}/login", json={"username": username, "password": PASSWORD})
check("login returns 200", r.status_code == 200, r.text)
token = r.json().get("token")
check("login issues a session token", bool(token))
auth = {"Authorization": f"Bearer {token}"}
r = requests.post(f"{BASE}/login", json={"username": username, "password": "wrong-password"})
check("wrong password -> 401", r.status_code == 401, f"got {r.status_code}")

# 4. Ingest text (local NLP fallback when OPENAI_API_KEY is empty)
payload = {
    "content": "Submit the history essay tomorrow at 4pm. Remind me to call Mom on Friday.",
    "user_id": user_id,
    "local_time": "2026-06-11T09:00:00 (Local Time)",
}
r = requests.post(f"{BASE}/ingest", json=payload, headers=auth)
check("ingest text returns 200", r.status_code == 200, r.text)
first_ids = r.json().get("task_ids", [])
check("ingest extracted at least 1 task", len(first_ids) >= 1, r.text)

# 5. Re-ingest the same text — cross-source dedup should reuse the same tasks
r = requests.post(f"{BASE}/ingest", json=payload, headers=auth)
second_ids = r.json().get("task_ids", [])
check("re-ingest dedupes to the same task ids", set(second_ids) <= set(first_ids) and len(second_ids) >= 1,
      f"first={first_ids} second={second_ids}")

# 6. Task list is readable with auth, and only own tasks
r = requests.get(f"{BASE}/tasks", params={"user_id": user_id}, headers=auth)
check("GET /tasks with token -> 200", r.status_code == 200, r.text)
r = requests.get(f"{BASE}/tasks", params={"user_id": user_id + 999}, headers=auth)
check("GET another user's tasks -> 403", r.status_code == 403, f"got {r.status_code}")

# 7. Insights summary (rules-based without an OpenAI key)
r = requests.get(f"{BASE}/insights/summary", params={"user_id": user_id, "local_time": "2026-06-11T09:00:00"}, headers=auth)
check("insights summary returns 200", r.status_code == 200, r.text)
body = r.json()
check("insights summary has text", bool(body.get("summary")), str(body))
check("insights stats counted active tasks", body.get("stats", {}).get("active", 0) >= 1, str(body))

# 8. Voice-note upload path rejects gracefully without an OpenAI key
r = requests.post(
    f"{BASE}/ingest-doc",
    data={"user_id": str(user_id)},
    files={"file": ("note.mp3", b"fake-audio-bytes", "audio/mpeg")},
    headers=auth,
)
check("voice note without OpenAI key -> 503", r.status_code == 503, f"got {r.status_code}: {r.text}")

# 9. Text file upload works end to end
r = requests.post(
    f"{BASE}/ingest-doc",
    data={"user_id": str(user_id), "local_time": "2026-06-11T09:00:00 (Local Time)"},
    files={"file": ("todo.txt", b"Buy groceries on 07/15. Finish the budget report by Friday.", "text/plain")},
    headers=auth,
)
check("txt upload returns 200", r.status_code == 200, r.text)
check("txt upload extracted tasks", len(r.json().get("task_ids", [])) >= 1, r.text)

# 10. Static pages
for path in ("/", "/terms.html", "/privacy.html"):
    r = requests.get(f"{BASE}{path}")
    check(f"GET {path} -> 200", r.status_code == 200, f"got {r.status_code}")

# 11. Logout invalidates the token
r = requests.post(f"{BASE}/logout", headers=auth)
check("logout returns 200", r.status_code == 200, r.text)
r = requests.get(f"{BASE}/tasks", params={"user_id": user_id}, headers=auth)
check("token rejected after logout", r.status_code == 401, f"got {r.status_code}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All smoke checks passed.")
