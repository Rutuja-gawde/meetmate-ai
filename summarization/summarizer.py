import sys
import ollama
import json
import re

SYSTEM_PROMPT = """You are a meeting summarization engine. You output ONLY raw JSON — no markdown fences, no commentary, no explanations before or after. If you include anything other than the JSON object, you have failed the task."""

def build_prompt(transcript: str) -> str:
    return f"""Given the meeting transcript below, return a JSON object with EXACTLY this structure:

{{
  "summary": "A detailed 5-7 sentence summary covering the main topics, outcomes and overall tone of the meeting",
  "agenda": ["Topic 1: one-line note on what was discussed", "Topic 2: one-line note on what was discussed"],
  "decisions": ["decision 1", "decision 2"],
  "action_items": [
    {{"task": "description", "owner": "name or 'unassigned'", "due_date": "YYYY-MM-DD or 'unspecified'"}}
  ],
  "deadlines": ["deadline 1", "deadline 2"]
    }}
Rules:
- Output ONLY the JSON object. No ```json fences. No leading/trailing text.
- If a field has no data, use an empty list [] — never omit the key.
- Infer due_date only if explicitly stated or clearly implied (e.g. "next Friday"); otherwise use "unspecified".
- Do NOT invent specific calendar dates (like YYYY-MM-DD) unless the transcript explicitly gives one. If only a relative day is mentioned (e.g. "Wednesday", "next Friday"), use that exact phrase as due_date instead of guessing a date.
- Do NOT include topics as "decisions" if the transcript explicitly says no decision was made (e.g. "no decision on X" must NOT appear in decisions).
- owner must be a person mentioned in the transcript, or "unassigned".
- "agenda" must list every distinct topic discussed, in the order it came up, each with a short note on what was said about it.
- "summary" must be 5-7 sentences, written in plain English even if the transcript mixes Hindi or Marathi.

Transcript:
\"\"\"
{transcript}
\"\"\"
"""

def extract_json(raw_text: str) -> dict:
    """Strip common wrapper artifacts and parse JSON, with a fallback regex extraction."""
    text = raw_text.strip()

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse JSON even after extraction. Raw output:\n{raw_text}") from e
    raise ValueError(f"No JSON object found in model output. Raw output:\n{raw_text}")

def choose_context_size(prompt: str) -> int:
    """Picks how much text the model can read at once.

    Ollama's default window is small, and anything longer is silently cut off,
    so the end of a long meeting would be missing from the summary. The estimate
    is deliberately generous because Hindi and Marathi text uses more tokens per
    character than English. A smaller window is faster, so the smallest one that
    fits is used."""
    estimated_tokens = len(prompt) // 2 + 2200   # transcript + room for the answer
    for size in (4096, 8192, 16384, 32768):
        if estimated_tokens <= size:
            return size
    return 32768

def summarize_transcript(transcript: str, model: str = "llama3.1:8b", retries: int = 2) -> dict:
    prompt = build_prompt(transcript)
    num_ctx = choose_context_size(prompt)

    last_error = None
    for attempt in range(retries + 1):
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format="json",
            options={"temperature": 0.1, "num_ctx": num_ctx, "num_predict": 2048},
            keep_alive="30m",  # keep the model loaded so the next meeting starts faster
        )
        raw = response["message"]["content"]
        try:
            return extract_json(raw)
        except ValueError as e:
            last_error = e
            continue

    raise last_error


if __name__ == "__main__":
    # ---- INPUT: read transcript from a file path passed as command-line argument ----
    if len(sys.argv) < 2:
        print("Usage: python summarizer.py <transcript_file.txt>")
        sys.exit(1)

    input_path = sys.argv[1]

    with open(input_path, "r", encoding="utf-8") as f:
        transcript = f.read()

    result = summarize_transcript(transcript)

    # ---- OUTPUT: print to console (for visibility) AND save to summary.json ----
    print(json.dumps(result, indent=2))

    with open("summary.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("\nSaved output to summary.json")