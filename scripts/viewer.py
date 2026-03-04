import sqlite3
import os

def view_database():
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    DB_PATH = os.path.join(BASE_DIR, "data", "clerk.db")
    
    print(f"🔍 DEBUG: Absolute path used: {DB_PATH}")

    if not os.path.exists(DB_PATH):
        print(f"❌ Error: Database NOT FOUND at {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n--- RAW INPUTS TABLE ---")
    cursor.execute("SELECT * FROM raw_inputs")
    rows = cursor.fetchall()
    for row in rows:
        print(row)

    print("\n--- TASKS TABLE ---")
    cursor.execute("SELECT * FROM tasks")
    tasks = cursor.fetchall()
    if not tasks:
        print("(No tasks found yet)")
    for task in tasks:
        print(task)

    conn.close()

if __name__ == "__main__":
    view_database()