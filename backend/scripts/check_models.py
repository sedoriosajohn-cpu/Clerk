import os
import sys
from openai import OpenAI
from dotenv import load_dotenv

# 1. Force a print to prove the script is actually running
print("--- Starting Model Check Script ---")

# 2. Check if .env actually exists
if not os.path.exists(".env"):
    print("Warning: No .env file found in the current directory.")

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("Error: OPENAI_API_KEY not found in environment variables.")
    sys.exit()

print(f"API Key found (starts with {api_key[:7]}...)")

client = OpenAI(api_key=api_key)

try:
    print("Connecting to OpenAI...")
    models = client.models.list()
    
    print("\nConnection Successful! Searching for GPT-5 models...")
    
    found_any = False
    for model in models:
        if "gpt" in model.id.lower():
            print(f" - {model.id}")
            found_any = True
    
    if not found_any:
        print("No GPT models found. Here is the first model in your list instead:")
        print(f" - {models.data[0].id}")

except Exception as e:
    print(f"API Error: {e}")

print("\n--- Script Finished ---")