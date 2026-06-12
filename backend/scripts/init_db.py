import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime

load_dotenv()
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATABASE_URL = os.getenv("DATABASE_URL")

def _sqlite_fallback():
    local_db_path = os.path.join(PROJECT_ROOT, "clerk.db")
    url = f"sqlite:///{local_db_path.replace(os.sep, '/')}"
    return url, local_db_path

if not DATABASE_URL:
    DATABASE_URL, _local_db_path = _sqlite_fallback()
    print(f"DATABASE_URL not set; using local SQLite database at {_local_db_path}")
else:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

try:
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
    # Quick connectivity test so we fail fast rather than at first request.
    if not DATABASE_URL.startswith("sqlite"):
        with engine.connect():
            pass
except Exception as _db_err:
    print(f"⚠️  Could not connect to the configured database ({_db_err}).")
    print("   Falling back to local SQLite database.")
    DATABASE_URL, _local_db_path = _sqlite_fallback()
    connect_args = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event as _sa_event
    @_sa_event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA cache_size=-32000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

#Define Table Structures
class User(Base):
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True)
    username = Column(String, unique=True)
    password_hash = Column(String)
    email = Column(String)
    preferred_name = Column(String)
    schedule_match_name = Column(String)
    preferred_work_start_hour = Column(Integer, default=9)
    preferred_work_end_hour = Column(Integer, default=17)
    dark_mode = Column(Integer, default=0)
    notifications_enabled = Column(Integer, default=1)
    two_factor_enabled = Column(Integer, default=0)
    two_factor_code_hash = Column(String)
    two_factor_expires_at = Column(String)
    reset_password_token_hash = Column(String)
    reset_password_expires_at = Column(String)
    google_token_json = Column(Text)  # Stores OAuth token JSON (replaces filesystem file)
    google_sub = Column(String)
    api_token_hash = Column(String)  # SHA-256 of the current session token; null = signed out
    timezone_name = Column(String)   # IANA tz (e.g. "America/New_York") for DST-correct sync conversions

class RawInput(Base):
    __tablename__ = "raw_inputs"
    raw_id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    source_type = Column(String, default="text")
    source_id = Column(String)
    received_at = Column(DateTime, default=datetime.utcnow)

class Task(Base):
    __tablename__ = "tasks"
    owner_id = Column(Integer, ForeignKey('users.user_id'))
    task_id = Column(Integer, primary_key=True, index=True)
    raw_id = Column(Integer, ForeignKey("raw_inputs.raw_id"))
    title = Column(String, nullable=False)
    description = Column(Text)
    due_date = Column(String) 
    end_date = Column(String) # For spanning time slots
    due_text = Column(String)
    assignee = Column(String, default="me")
    item_type = Column(String, default="task") # 'task' or 'reminder'
    priority = Column(String, default="normal")
    is_all_day = Column(Integer, default=0)
    confidence = Column(Float)
    status = Column(String, default="pending")
    user_feedback = Column(Integer, nullable=True)  # +1 correct, -1 incorrect, None = no feedback
    created_at = Column(DateTime, default=datetime.utcnow)

def ensure_database_schema():
    if not DATABASE_URL:
        return

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "tasks" not in table_names or "users" not in table_names:
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)

    existing_columns = {column["name"] for column in inspector.get_columns("tasks")}
    task_columns_to_add = {
        "description": "TEXT",
        "user_feedback": "INTEGER",
    }
    with engine.begin() as connection:
        for col_name, col_type in task_columns_to_add.items():
            if col_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}"))

    existing_user_columns = {column["name"] for column in inspector.get_columns("users")}
    user_columns = {
        "email": "VARCHAR",
        "schedule_match_name": "VARCHAR",
        "preferred_work_start_hour": "INTEGER DEFAULT 9",
        "preferred_work_end_hour": "INTEGER DEFAULT 17",
        "two_factor_enabled": "INTEGER DEFAULT 0",
        "two_factor_code_hash": "VARCHAR",
        "two_factor_expires_at": "VARCHAR",
        "reset_password_token_hash": "VARCHAR",
        "reset_password_expires_at": "VARCHAR",
        "google_token_json": "TEXT",
        "api_token_hash": "VARCHAR",
        "timezone_name": "VARCHAR",
    }
    user_columns["google_sub"] = "VARCHAR"
    with engine.begin() as connection:
        for column_name, column_type in user_columns.items():
            if column_name not in existing_user_columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"))

#Initialization Function
def initialize_database():
    if not DATABASE_URL:
        print("Missing DATABASE_URL!")
        return

    print("Connecting to database...")
    try:
        Base.metadata.create_all(bind=engine)
        ensure_database_schema()
        print("✅ Tables verified/created.")
    except Exception as e:
        print(f"Failed to initialize database: {e}")

if __name__ == "__main__":
    initialize_database()
