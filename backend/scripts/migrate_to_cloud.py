import sqlite3
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
# Import your classes from your updated init_db.py
from init_db import RawInput, Task 

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("Error: DATABASE_URL not found in .env file!")
    exit(1)

#sets up cloud connection
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
cloud_session = Session()

#sets up local connection
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB_PATH = os.path.join(BASE_DIR, "data", "clerk.db")

def migrate():
    if not os.path.exists(LOCAL_DB_PATH):
        print(f"Error: Could not find local database at {LOCAL_DB_PATH}")
        return

    print("Starting migration to Neon Cloud...")
    local_conn = sqlite3.connect(LOCAL_DB_PATH)
    local_cursor = local_conn.cursor()

    try:
        # --- Migrate Raw Inputs ---
        print("Migrating raw_inputs...")
        local_cursor.execute("SELECT content, source_type, source_id, received_at FROM raw_inputs")
        for row in local_cursor.fetchall():
            new_input = RawInput(
                content=row[0],
                source_type=row[1],
                source_id=row[2],
                received_at=row[3] 
            )
            cloud_session.add(new_input)

        print("Migrating tasks...")
        local_cursor.execute("SELECT raw_id, title, due_date, due_text, assignee, priority, confidence, status, created_at FROM tasks")
        for row in local_cursor.fetchall():
            new_task = Task(
                raw_id=row[0],
                title=row[1],
                due_date=str(row[2]) if row[2] else None,
                due_text=row[3],
                assignee=row[4],
                priority=row[5],
                confidence=row[6],
                status=row[7],
                created_at=row[8]
            )
            cloud_session.add(new_task)

        cloud_session.commit()
        print("Migration complete! Your cloud database is now up to date.")
        
    except Exception as e:
        print(f"Error during migration: {e}")
        cloud_session.rollback()
    finally:
        local_conn.close()
        cloud_session.close()

if __name__ == "__main__":
    migrate()