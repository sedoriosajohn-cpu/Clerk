import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

try:
    models = client.models.list()
    print("Connection Successful! Your available models are:")
    for model in models:
        if "gpt-5.4" in model.id:
            print(f" - {model.id}")
except Exception as e:
    print(f"Error: {e}")