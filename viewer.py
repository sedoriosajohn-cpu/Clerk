import sqlite3

def view_all_tasks():
    # 1. Connect to the same clerk.db
    conn = sqlite3.connect('clerk.db')
    cursor = conn.cursor()

    try:
        # 2. Query all tasks with their raw text context
        print("--- CURRENT TASKS IN DATABASE ---")
        query = """
            SELECT t.task_id, t.title, t.due_text, t.status, r.content
            FROM tasks t
            JOIN raw_inputs r ON t.raw_id = r.raw_id
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        if not rows:
            print("The database is currently empty.")
        else:
            for row in rows:
                print(f"ID: {row[0]} | Task: {row[1]} | Due: {row[2]} | Status: {row[3]}")
                print(f"   Source Text: \"{row[4]}\"")
                print("-" * 30)

    except sqlite3.Error as e:
        print(f"Error reading database: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    view_all_tasks()