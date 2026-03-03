import sqlite3
import json
from datetime import datetime

def save_task_to_db(raw_text, task_data):
    # 1. Connect to your database file
    conn = sqlite3.connect('clerk.db')
    cursor = conn.cursor()

    try:
        # 2. Insert the raw input first to get a raw_id
        cursor.execute(
            "INSERT INTO raw_inputs (content, source_type) VALUES (?, ?)",
            (raw_text, "text")
        )
        raw_id = cursor.lastrowid  # This gets the AUTOINCREMENT ID just created

        # 3. Insert the task using the data from the JSON-style object
        cursor.execute("""
            INSERT INTO tasks (
                raw_id, title, due_date, due_text, assignee, priority, confidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            raw_id,
            task_data['title'],
            task_data['due'],
            task_data['due_text'],
            task_data['assignee'],
            task_data['priority'],
            task_data['confidence'],
            "pending"
        ))

        # 4. Commit (save) the changes
        conn.commit()
        print(f"Successfully saved task: {task_data['title']}")

    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

example_raw = "Can you send the permission slips by Friday?"
example_task = {
    "title": "Send permission slips",
    "due": "2026-02-13",
    "due_text": "by Friday",
    "assignee": "me",
    "priority": "normal",
    "confidence": 0.82
}

if __name__ == "__main__":
    save_task_to_db(example_raw, example_task)