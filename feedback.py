import os
import json
import uuid
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

FEEDBACK_FILE = "<project directory>/.feedback.json"

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "shall",
    "i", "my", "we", "our", "you", "your", "it", "its", "not",
    "no", "and", "or", "but", "if", "then", "so", "as", "to",
    "of", "in", "for", "on", "with", "at", "by", "from", "this",
    "that", "these", "those", "how", "what", "when", "where", "why"
}

# Persists user feedback to disk and provides hint generation for future queries based on past negative feedback.
class feedbackStore:
    def __init__(self, path: str = FEEDBACK_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"positive": [], "negative": []}
        try:
            with open(self.path) as f:
                content = f.read().strip()
                if not content:
                    return {"positive": [], "negative": []}
                return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to load feedback store: {e}", exc_info=True)
            return {"positive": [], "negative": []}

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save feedback store: {e}", exc_info=True)

    def add_positive(self, user_id: str, question: str, answer: str, sources: list):
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "question": question,
            "answer": answer,
            "sources": sources
        }

        with self.lock:
            self.data["positive"].append(entry)
            self._save()
        logger.info(f"Stored positive feedback [{entry['id'][:8]}]")

    def add_negative(self, user_id: str, question: str, answer: str, sources: list, user_feedback: str, history: list = None) -> str:
        entry_id = str(uuid.uuid4())
        entry = {
            "id": entry_id,
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "question": question,
            "answer": answer,
            "sources": sources,
            "user_feedback": user_feedback,
            "history": history or []
        }

        with self.lock:
            self.data["negative"].append(entry)
            self._save()
        logger.info(f"Stored negative feedback [{entry_id[:8]}]:'{user_feedback[:60]}'")
        return entry_id

    # Similarity is determined by keyword overlap — user question needs at least 2 meaningful words in common with a past negatively rated question.
    def get_feedback(self, question: str) -> str:
        with self.lock:
            negative = list(self.data["negative"])

        if not negative:
            return ""

        question_words = self.extract_keywords(question)
        if not question_words:
            return ""

        relevant_feedback = []
        for entry in negative:
            if not entry.get("user_feedback"):
                continue
            past_words = self.extract_keywords(entry["question"])
            overlap = question_words & past_words
            if len(overlap) >= 2:
                relevant_feedback.append(entry["user_feedback"])

        if not relevant_feedback:
            return ""

        # Deduplicate and cap at 3 hints to avoid bloating the prompt
        unique = list(dict.fromkeys(relevant_feedback))[:3]
        return ("Important note: Previous users found answers to similar questions unhelpful. Their feedback was:\n" + "\n".join(f"- {f}" for f in unique)
            + "\nPlease take this into account and ensure your answer addresses these concerns where relevant.")

    def extract_keywords(self, text: str) -> set:
        return {
            w.strip("?.,!:;'\"").lower()
            for w in text.split()
            if len(w.strip("?.,!:;'\"")) > 2
            and w.strip("?.,!:;'\"").lower() not in STOPWORDS
        }

    def get_stats(self) -> dict:
        with self.lock:
            return {
                "positive": len(self.data["positive"]),
                "negative": len(self.data["negative"])
            }
