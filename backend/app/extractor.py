import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extract_task_from_text(text: str):
    """
    This function takes raw text and returns a dictionary 
    of structured task data using GPT-4.
    """
    
    prompt = f"""
    You are an AI task extraction engine for a productivity app called Clerk.

    Extract one task from the user's message and return ONLY valid JSON in this format:
    {{
      "title": "short task title",
      "description": "expanded description",
      "due": "human-readable due date",
      "time": "human-readable scheduled time",
      "priority": "High, Medium, or Normal",
      "confidence": 85
    }}

    Rules:
    - Infer the most likely task.
    - Keep title concise and professional.
    - If a deadline is vague, make a reasonable demo-friendly assumption.
    - If urgency is strong, set priority to High.
    - Confidence should be an integer from 0 to 100.

    User input:
    {text}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-5o",
            messages=[{"role": "system", "content": "You are a helpful assistant that outputs JSON."},
                      {"role": "user", "content": prompt}],
            response_format={ "type": "json_object" } 
        )
        
        raw_json = response.choices[0].message.content
        
        task_data = json.loads(raw_json)
        return task_data

    except Exception as e:
        print(f"Error calling OpenAI: {e}")
        return None