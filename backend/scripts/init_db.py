import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
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
class User(Base):
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True)
    username = Column(String, unique=True)
    password_hash = Column(String)

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
        print("Missing DATABASE_URL!")
        return

    print(f"Connecting to database...")
    
    try:
        # 1. Create the tables based on your Classes (User, Task, etc.)
        Base.metadata.create_all(bind=engine)
        print("✅ Tables verified/created.")

        # 2. Open a temporary session to add the admin user
        db = SessionLocal()
        try:
            # Check if 'admin' already exists so we don't create duplicates
            existing_user = db.query(User).filter(User.username == "admin").first()
            
            if not existing_user:
                print("Seeding database with admin user...")
                admin_user = User(
                    username="admin", 
                    password_hash="MTLIKESRACHEL"  # In production, use a proper password hashing function!
                )
                db.add(admin_user)
                db.commit()
                print("✅ Admin user created successfully!")
            else:
                print("ℹ️ Admin user already exists, skipping seed.")
        finally:
            db.close()

    except Exception as e:
        print(f"Failed to initialize database: {e}")

if __name__ == "__main__":
    initialize_database()