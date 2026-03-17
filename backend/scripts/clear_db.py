import sqlite3
import os

# Get the path to the database
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "clerk.db")

def clear_database():
    confirm = input("This will delete ALL tasks and raw inputs. Are you sure? (y/n): ")
    if confirm.lower() != 'y':
        print("Aborted.")
        return

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Delete from both tables
            cursor.execute("DELETE FROM tasks")
            cursor.execute("DELETE FROM raw_inputs")
            # Reset the ID counters so the next task starts at ID 1
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='tasks'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='raw_inputs'")
            conn.commit()
            print("✅ Database cleared successfully!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    clear_database()