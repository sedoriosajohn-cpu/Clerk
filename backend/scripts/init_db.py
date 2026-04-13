import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("Error: DATABASE_URL not found in .env file!")
    # For local debugging only, uncomment if needed:
    # DATABASE_URL = "sqlite:///./clerk.db" 
else:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Base = declarative_base()

    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(ROOT_DIR, "data")
    DB_PATH = os.path.join(DATA_DIR, "clerk.db")

class Task(Base):
    __tablename__ = "tasks"
    task_id = Column(Integer, primary_key=True, index=True)
    raw_id = Column(Integer, ForeignKey("raw_inputs.raw_id"))
    title = Column(String, nullable=False)
    due_date = Column(String) 
    due_text = Column(String)
    assignee = Column(String, default="me")
    priority = Column(String, default="normal")
    confidence = Column(Float)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

    if not os.path.exists(DATA_DIR):
        #Creates the data directory if it doesn't exist
        os.makedirs(DATA_DIR, exist_ok=True)
        print(f"Error: No database found at {DATA_DIR}")
        return

    print(f"Connecting to: {DATABASE_URL.split('@')[-1]}")
    
    try:
        engine = create_engine(DATABASE_URL)
        Base.metadata.create_all(bind=engine)
        print("✅ Success: Cloud database tables created/verified!")
    except Exception as e:
        print(f"Failed to initialize database: {e}")

if __name__ == "__main__":
    initialize_database()