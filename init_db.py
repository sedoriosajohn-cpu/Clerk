import sqlite3
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS raw_inputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            source_type TEXT DEFAULT 'text',
            source_id TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
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
    ''')
    conn.commit()
    conn.close()
    print("Database initialized successfully with tables: raw_inputs, tasks")
    
if __name__ == "__main__":
    init_db()