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
        
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

#Define Table Structures
class RawInput(Base):
    __tablename__ = "raw_inputs"
    raw_id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    source_type = Column(String, default="text")
    source_id = Column(String)
    received_at = Column(DateTime, default=datetime.utcnow)

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

#Initialization Function
def initialize_database():
    if not DATABASE_URL:
        return

    print(f"Connecting to: {DATABASE_URL.split('@')[-1]}")
    
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Success: Cloud database tables created/verified!")
    except Exception as e:
        print(f"Failed to initialize database: {e}")

if __name__ == "__main__":
    initialize_database()