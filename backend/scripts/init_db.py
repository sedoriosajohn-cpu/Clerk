import sqlite3
import os

def initialize_database():
    #Determines the path of the database file relative to this script 
    #This makes it more robust across different environments and setups

    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(ROOT_DIR, "data")
    DB_PATH = os.path.join(DATA_DIR, "clerk.db")

    print(f"DEBUG: Looking for database at: {DB_PATH}")

    if not os.path.exists(DATA_DIR):
        #Creates the data directory if it doesn't exist
        os.makedirs(DATA_DIR, exist_ok=True)
        print(f"Error: No database found at {DATA_DIR}")
        return
    #Connects to the SQLite database and creates the necessary tables if they don't already exist
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_inputs (
        raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        source_type TEXT DEFAULT 'text',
        source_id TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_id INTEGER,
        title TEXT NOT NULL,
        due_date DATE,
        due_text TEXT,
        assignee TEXT DEFAULT 'me',
        priority TEXT DEFAULT 'normal',
        confidence REAL,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (raw_id) REFERENCES raw_inputs(raw_id)
    );
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized at: {DB_PATH}")

if __name__ == "__main__":
    initialize_database()