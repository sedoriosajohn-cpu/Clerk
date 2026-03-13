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

function buildPrompt(userInput, currentTime) {
  return `
You are an AI task extraction engine for a productivity app called Clerk.

Current UTC Timestamp: ${currentTime}

Extract ONE actionable task from the user's input.

Return ONLY valid JSON in this exact format:
{
  "title": "short task title",
  "description": "expanded description",
  "due_date": "ISO 8601 timestamp in UTC or null",
  "assignee": "name or null",
  "priority": "low | normal | high",
  "confidence": 0
}

Rules:
- Extract only one real actionable task.
- Keep the title concise and professional.
- Expand the description clearly.
- Convert relative dates like "next Friday" or "tomorrow at 5pm" into an exact ISO 8601 UTC timestamp.
- If there is no due date, return null.
- If no assignee is mentioned, return null.
- Priority must be exactly one of: low, normal, high.
- Confidence must be an integer from 0 to 100.

Confidence guide:
- 90-100 = explicit obligation
- 70-89 = clear task
- 40-69 = inferred or tentative task
- 0-39 = not really a task

User input:
${userInput}
`;
}

function extractJson(text) {
  const trimmed = text.trim();

  try {
    return JSON.parse(trimmed);
  } catch {
    const match = trimmed.match(/\{[\s\S]*\}/);
    if (!match) {
      throw new Error("Model did not return valid JSON.");
    }
    return JSON.parse(match[0]);
  }
}

function normalizePriority(priority) {
  if (!priority) return "normal";

  const p = String(priority).toLowerCase().trim();

  if (["high", "urgent", "important"].includes(p)) return "high";
  if (["low", "minor"].includes(p)) return "low";
  return "normal";
}

function isValidISODate(value) {
  if (value === null) return true;
  return !Number.isNaN(Date.parse(value));
}

function validateTask(task) {
  if (!task || typeof task !== "object") {
    throw new Error("Task output is not an object.");
  }

  const validated = {
    title: typeof task.title === "string" ? task.title.trim() : "",
    description:
      typeof task.description === "string" ? task.description.trim() : "",
    due_date: task.due_date ?? null,
    assignee:
      typeof task.assignee === "string" && task.assignee.trim()
        ? task.assignee.trim()
        : null,
    priority: normalizePriority(task.priority),
    confidence: Number.isInteger(task.confidence)
      ? task.confidence
      : parseInt(task.confidence, 10),
  };

  if (!validated.title) {
    throw new Error("Missing title.");
  }

  if (!validated.description) {
    throw new Error("Missing description.");
  }

  if (!Number.isFinite(validated.confidence)) {
    throw new Error("Confidence must be a number.");
  }

  validated.confidence = Math.max(0, Math.min(100, validated.confidence));

  if (!isValidISODate(validated.due_date)) {
    throw new Error("due_date must be ISO 8601 or null.");
  }

  return validated;
}

function adjustConfidence(userInput, task) {
  const input = userInput.toLowerCase();

  const strongWords = [
    "submit",
    "send",
    "finish",
    "complete",
    "turn in",
    "due",
    "deadline",
    "must",
    "need to",
  ];

  const weakWords = [
    "maybe",
    "might",
    "should",
    "probably",
    "look into",
    "consider",
    "possibly",
  ];

  let score = task.confidence;

  if (strongWords.some((word) => input.includes(word))) {
    score += 5;
  }

  if (weakWords.some((word) => input.includes(word))) {
    score -= 15;
  }

  task.confidence = Math.max(0, Math.min(100, score));
  return task;
}

function formatForFrontend(task) {
  return {
    ...task,
    due: task.due_date
      ? new Date(task.due_date).toLocaleDateString()
      : "No due date",
    time: task.due_date
      ? new Date(task.due_date).toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
        })
      : "No time",
  };
}

app.post("/extract-task", async (req, res) => {
  try {
    const { text } = req.body;

    if (!text || !text.trim()) {
      return res.status(400).json({ error: "Missing task text." });
    }

    const nowIso = new Date().toISOString();
    const prompt = buildPrompt(text, nowIso);

    const response = await openai.responses.create({
      model: "gpt-5.4",
      input: prompt,
    });

    const raw = response.output_text?.trim();

    if (!raw) {
      throw new Error("Empty response from model.");
    }

    let task = extractJson(raw);
    task = validateTask(task);
    task = adjustConfidence(text, task);

    const frontendTask = formatForFrontend(task);

    res.json({
      raw_input: text,
      extracted_at: nowIso,
      task: frontendTask,
    });
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