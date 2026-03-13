import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import OpenAI from "openai";

dotenv.config();

const app = express();
const port = 3000;

app.use(cors());
app.use(express.json());
app.use(express.static("."));

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

app.post("/extract-task", async (req, res) => {
  try {
    const { text } = req.body;

    if (!text || !text.trim()) {
      return res.status(400).json({ error: "Missing task text." });
    }

    const prompt = `
You are an AI task extraction engine for a productivity app called Clerk.

Extract one task from the user's message and return ONLY valid JSON in this format:
{
  "title": "short task title",
  "description": "expanded description",
  "due": "human-readable due date",
  "time": "human-readable scheduled time",
  "priority": "High, Medium, or Normal",
  "confidence": "0-100"
}

Rules:
- Infer the most likely task.
- Keep title concise and professional.
- If a deadline is vague, make a reasonable demo-friendly assumption.
- If urgency is strong, set priority to High.
- Confidence should be an integer from 0 to 100.

User input:
${text}
`;

    const response = await openai.responses.create({
      model: "gpt-5.4",
      input: prompt,
    });

    const raw = response.output_text.trim();
    const parsed = JSON.parse(raw);

    res.json(parsed);
  } catch (error) {
    console.error("OpenAI error:", error);
    res.status(500).json({
      error: "Failed to process task.",
      details: error.message,
    });
  }
});

app.listen(port, () => {
  console.log(`Server running at http://localhost:${port}`);
});