import sqlite3
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "clerk.db")

def save_task_to_db(raw_text, task_data):
    if not os.path.exists(os.path.dirname(DB_PATH)):
        print(f"Error: Data directory not found at {os.path.dirname(DB_PATH)}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO raw_inputs (content, source_type) VALUES (?, ?)",
            (raw_text, "text")
        )
        raw_id = cursor.lastrowid 
        
        cursor.execute("""
            INSERT INTO tasks (
                raw_id, title, due_date, due_text, assignee, priority, confidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            raw_id,
            task_data.get('title'),
            task_data.get('due'),
            task_data.get('due_text'),
            task_data.get('assignee', 'me'),
            task_data.get('priority', 'normal'),
            task_data.get('confidence', 0.0),
            "pending"
        ))

        conn.commit()
        print(f"Successfully linked and saved task: {task_data['title']} (Raw ID: {raw_id})")

    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
    finally:
        conn.close()

# --- TEST DATA ---
example_raw = "Can you send the permission slips by Friday?"
example_task = {
    "title": "Send permission slips",
    "due": "2026-03-06",
    "due_text": "by Friday",
    "assignee": "me",
    "priority": "normal",
    "confidence": 0.82
}

if __name__ == "__main__":
    # To run this, type: python app/cleaner.py
    save_task_to_db(example_raw, example_task)