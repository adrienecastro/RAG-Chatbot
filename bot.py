import os
import re
import time
import uuid
import hashlib
import logging
import tempfile
import threading
from collections import defaultdict
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from rag import load_index, query
from ingest import run_ingest
from extract_images import pdf_images
from chat_history import chatHistory
from error_logging import error_logging
from feedback import feedbackStore
from slack_survey import *


# Sets env variables and bot logging
load_dotenv()
error_logging()
logger = logging.getLogger(__name__)
app = App(token=os.getenv("SLACK_BOT_TOKEN"))
feedback_store = feedbackStore()


# Sets ttl for user chat history
chat_history = chatHistory(max_turns=10, ttl_seconds=1800)


# Slack admin verification
raw_admin_ids = os.getenv("ADMIN_SLACK_USER_IDS", "")
ADMIN_IDS = {uid.strip() for uid in raw_admin_ids.split(",") if uid.strip()}
if not ADMIN_IDS:
    logger.warning("ADMIN_SLACK_USER_IDS is not set — /reload will be unavailable.")


# Rate Limiter
class rateLimiter:
    def __init__(self, max_calls: int = 10, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls = defaultdict(list)
        self._lock = threading.Lock()

    def rate_allowed(self, user_id: str) -> bool:
        now = time.time()
        with self._lock:
            self._calls[user_id] = [t for t in self._calls[user_id] if now - t < self.window_seconds]

            if len(self._calls[user_id]) >= self.max_calls:
                return False

            self._calls[user_id].append(now)
            return True

rate_limiter = rateLimiter(max_calls=10, window_seconds=60)


# Input Sanitization
MAX_QUESTION_LENGTH = 1000

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"forget\s+(everything|all|your)",
    r"you\s+are\s+now\s+a",
    r"act\s+as\s+(if\s+you\s+are|a)",
    r"jailbreak",
    r"dan\s+mode",
    r"pretend\s+you",
    r"system\s*prompt",
    r"reveal\s+your\s+(instructions|prompt|system)",
]

def sanitize_input(text: str) -> tuple:
    text = text[:MAX_QUESTION_LENGTH]
    text_lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            logger.warning(f"Potential prompt injection detected: '{text[:100]}'")
            return text, True
    return text.strip(), False


# Hashes user query
def hash_query(question: str) -> str:
    query_hash = hashlib.sha256(question.encode()).hexdigest()[:8]
    truncated = (question[:50] + "..." if len(question) > 50 else question)
    return f"[{query_hash}] {truncated}"


# Prevent path traversal
def prevent_traversal(filepath: str) -> bool:
    if not filepath:
        return False

    allowed_path = ["/opt/KeyWatchBot/"]

    resolved = os.path.realpath(filepath)

    logger.info(f"DEBUG prevent_traversal: checking '{resolved}'")

    for image in allowed_path:
        resolved_image = os.path.realpath(image)
        if resolved.startswith(resolved_image + os.sep):
            return True

    logger.warning(f"Blocked unsafe file path access attempt")
    return False


# Extract keywords from a question for image scoring
def detect_keywords(question: str) -> list:
    stopwords = {
        "a", "an", "the", "is", "are", "was", "were", "show", "me",
        "image", "picture", "photo", "diagram", "of", "in", "for",
        "to", "how", "can", "you", "please", "from", "what", "when",
        "where", "why", "do", "does", "i", "it", "this", "that",
        "with", "and", "or", "on", "at", "by", "be", "my", "we"
    }
    return [w for w in question.lower().split() if w not in stopwords and len(w) > 2]


#Product Detection
def get_products() -> list:
    raw_paths = os.getenv("DOCS_PATHS", "")
    scoped_keywords = ["User Manuals", "Miscellaneous"]
    products = []

    for path in raw_paths.split(","):
        path = path.strip()
        if any(keyword in path for keyword in scoped_keywords):
            if os.path.exists(path):
                subfolders = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
                products.extend(subfolders)

    return products

PRODUCTS = get_products()
logger.info(f"Known products loaded: {len(PRODUCTS)} products")

def detect_product(question: str) -> str:
    question_lower = question.lower()
    for product in PRODUCTS:
        if product.lower() in question_lower:
            return product
    return None


# Smalltack Detection
GREETINGS = [
    r"^hello\b", r"^hi\b", r"^hey\b", r"^howdy\b",
    r"^good morning\b", r"^good afternoon\b", r"^good evening\b",
    r"^what('s| is) up\b", r"^sup\b"
]
THANKS = [
    r"^thank(s| you)\b", r"^ty\b", r"^cheers\b", r"^appreciated\b"
]
FAREWELLS = [
    r"^bye\b", r"^goodbye\b", r"^see you\b", r"^later\b", r"^cya\b"
]


def detect_smalltalk(text: str) -> str:
    text_lower = text.lower().strip()
    for pattern in GREETINGS:
        if re.match(pattern, text_lower):
            return "Hey there! :wave: Ask me anything IT related and I'll do my best to help."

    for pattern in THANKS:
        if re.match(pattern, text_lower):
            return "Happy to help! :slightly_smiling_face: Let me know if you have any other questions."

    for pattern in FAREWELLS:
        if re.match(pattern, text_lower):
            return "Take care! :wave: Ask me anytime you need help."
    return None


# Detect image request
IMAGE_REQUEST_PATTERNS = [
    r"show\s+me\s+(a\s+)?(picture|image|photo|screenshot|diagram|figure)",
    r"(picture|image|photo|screenshot|diagram|figure)\s+of",
    r"what\s+does\s+.+\s+look\s+like",
    r"can\s+you\s+show",
    r"display\s+(a\s+)?(picture|image|photo|screenshot|diagram|figure)",
]

def detect_image_request(text: str) -> bool:
    text_lower = text.lower()
    for pattern in IMAGE_REQUEST_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


# Handle explicit image request
def handle_image_request(question: str, channel_id: str, say, client):
    try:
        index = load_index()
        retriever = index.as_retriever(similarity_top_k=6)
        nodes = retriever.retrieve(question)
    except Exception as e:
        logger.error(f"Retrieval failed in handle_image_request: {e}", exc_info=True)
        say(":x: Something went wrong retrieving documents. Please try again or contact a senior support agent.")
        return

    if not nodes:
        say("I couldn't find any relevant documents for that. Try mentioning the product or topic name more specifically. :)")
        return

    keywords = detect_keywords(question)

    retreived_pdfs = {}
    for node in nodes:
        name = node.metadata.get("display_name", "")
        filepath = node.metadata.get("file_path", "")
        url = node.metadata.get("drive_url")
        score = node.score or 0
        if name and filepath and name not in retreived_pdfs:
            retreived_pdfs[name] = {
                "filepath": filepath,
                "url": url,
                "score": score
            }

    retreived_pdfs = {
    name: info for name, info in retreived_pdfs.items() if info["filepath"].lower().endswith(".pdf")}

    if not retreived_pdfs:
        say("I couldn't find any PDFs with images for that. Try mentioning the product or topic name more specifically.")
        return

    sorted_pdfs = sorted(retreived_pdfs.items(), key=lambda x: x[1]["score"], reverse=True)
    pdf_filename, pdf_info = sorted_pdfs[0]
    pdf_filepath = pdf_info["filepath"]
    pdf_url = pdf_info["url"]

    # Validate path before opening
    if not os.path.exists(pdf_filepath) or not prevent_traversal(pdf_filepath):
        say(f"I found *{slack_escape(pdf_filename)}* but couldn't access it safely. Please try `/reload` and ask again.")
        return

    say(f":frame_with_picture: Pulling images from *{slack_escape(pdf_filename)}*...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            image_results = pdf_images(pdf_filepath, tmp_dir, keywords=keywords, max_images=5)
        except Exception as e:
            logger.error(f"Image extraction failed: {e}", exc_info=True)
            say(":x: Failed to extract images. Please check the server logs.")
            return

        logger.info(f"Extracted {len(image_results)} images for image request")

        if not image_results:
            if pdf_url:
                say(f"This document doesn't appear to have any images I can share. You can view it directly here: {slack_link(pdf_url, pdf_filename)}")
            else:
                say(f"This document doesn't appear to have any images I can share: *{slack_escape(pdf_filename)}*")
            return

        # Upload all images without image comments
        uploaded = 0
        for image_path, page_num in image_results:
            try:
                client.files_upload_v2(channel=channel_id, file=image_path, title=f"{pdf_filename} — page {page_num}")
                uploaded += 1
            except Exception as e:
                logger.error(f"Failed to upload image: {e}", exc_info=True)

        # Post source link for all uploaded images
        if uploaded > 0:
            if pdf_url:
                say(f"Source: {slack_link(pdf_url, pdf_filename)}")
        else:
            say(":x: Failed to upload images. Please check the server logs.")


# Event Handlers
@app.event("app_mention")
def handle_mention(event, say, client):
    user_question = event["text"].split(">", 1)[-1].strip()
    channel_id = event["channel"]
    user_id = event.get("user", "unknown")

    if not user_question:
        say("Hi! Ask me an IT question and I'll do my best to help.")
        return

    # Rate Limiting
    if not rate_limiter.rate_allowed(user_id):
        say(":hourglass: You're sending requests too fast! Please wait a moment before trying again.")
        return

    # Sanitize Input
    user_question, rejected = sanitize_input(user_question)
    if rejected:
        say( "lolz..\nGet rekt kid :p")
        return

    # Cancel any pending survey timer if user is still active
    cancel_timer(user_id)

    smalltalk_response = detect_smalltalk(user_question)
    if smalltalk_response:
        say(smalltalk_response)
        return

    # Explicit image request - bypasses RAG
    if detect_image_request(user_question):
        handle_image_request(user_question, channel_id, say, client)
        return

    # Log hashed queury
    logger.info(f"Processing query: {hash_query(user_question)}")

    say(":mag: Looking that up for you...")

    # Get user's history for this channel
    history = chat_history.get_history(user_id, channel_id)

    # Store the user message before querying
    chat_history.store_user_message(user_id, channel_id, user_question)

    # Get feedback hints from past negative feedback
    feedback = feedback_store.get_feedback(user_question)
    if feedback:
        logger.info(f"Applying feedback hints for query {hash_query(user_question)}")

    try:
        detected_product = detect_product(user_question)
        result = query(user_question, product=detected_product, history=history)
    except Exception as e:
        logger.error(f"Query failed: {e}", exc_info=True)
        say(":x: Something went wrong. Please try again or contact a senior support agent.")
        return

    # Store the bot's response in history
    chat_history.store_bot_message(user_id, channel_id, result["answer"])

    # Build and post answer
    source_lines = []
    for filename, url in result["sources"].items():
        category = result["categories"].get(filename, "")
        label = f"{category} — {filename}" if category else filename
        if url:
            source_lines.append(f"• {slack_link(url, label)}")
        else:
            source_lines.append(f"• {slack_escape(label)}")

    sources_text = ("\n".join(source_lines) if source_lines else "- No sources found -")

    # Post text answer and sources
    say(f"{result['answer']}\n\n*Sources:*\n{sources_text}")

    # Schedule survey in 30 minutes from now - timer resets if user messages again
    schedule_survey(
        user_id=user_id,
        channel_id=channel_id,
        question=user_question,
        answer=result["answer"],
        sources=result.get("sources", {}),
        history=chat_history.get_history(user_id, channel_id)
    )

    # Attempt image extraction from source PDFs
    PDF_MIN_IMAGE_SCORE = 0.45

    source_paths = result.get("source_paths", {})
    if not source_paths:
        return

    keywords = detect_keywords(user_question)

    source_scores = result.get("source_scores", {})

    with tempfile.TemporaryDirectory() as tmp_dir:
        total_uploaded = 0
        sources_with_uploads = []

        for filename, filepath in source_paths.items():
            if total_uploaded >= 3:
                break

            if not filepath or not os.path.exists(filepath):
                logger.warning("Source file not found on disk for image extraction")
                continue

            # Skip non-pdf files 
            if not filepath.lower().endswith(".pdf"):
                logger.info(f"Skipping image extraction for non-PDF: {filename}")
                continue

            # Skip low-confidence sources to avoid irrelevant images
            score = source_scores.get(filename, 0)
            if score < PDF_MIN_IMAGE_SCORE:
                logger.info(f"Skipping image extraction for low-confidence source ({score:.2f}): {filename}")
                continue

            # Path Validation
            if not prevent_traversal(filepath):
                logger.warning("Blocked unsafe path '{filepath}' during image extraction")
                continue

            remaining = 3 - total_uploaded
            try:
                image_results = pdf_images(filepath, tmp_dir, keywords=keywords, max_images=remaining)
            except Exception as e:
                logger.error(f"Image extraction failed: {e}", exc_info=True)
                continue

            if not image_results:
                continue

            for image_path, page_num in image_results:
                if total_uploaded >= 3:
                    break
                try:
                    client.files_upload_v2(channel=channel_id, file=image_path, title=f"{filename} - page {page_num}")
                    total_uploaded += 1
                    if filename not in sources_with_uploads:
                        sources_with_uploads.append(filename)
                except Exception as e:
                    logger.error(f"Failed to upload image: {e}", exc_info=True)

        # Post image source links after all images are uploaded
        if sources_with_uploads:
            link_lines = []
            for filename in sources_with_uploads:
                url = result["sources"].get(filename)
                if url:
                    link_lines.append(f"• {slack_link(url, filename)}")
                else:
                    link_lines.append(f"• {slack_escape(filename)}")
            say(f":frame_with_picture: Visual references:\n" + "\n".join(link_lines))


# Survey feedback handlers
@app.action("feedback_positive")
def positive_feedback(ack, body, client):
    ack()

    context_id = body["actions"][0]["value"]
    ctx = get_context(context_id)

    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Thanks for your feedback! ✅",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "✅ Thanks for the positive feedback! Glad I could help."
                        )
                    }
                }
            ]
        )
    except Exception as e:
        logger.error(f"Failed to update survey message: {e}", exc_info=True)

    if not ctx:
        logger.warning(f"Positive feedback received but context {context_id} not found (may have expired)")
        return

    feedback_store.add_positive(user_id=ctx["user_id"], question=ctx["question"], answer=ctx["answer"], sources=list(ctx["sources"].keys()))
    clear_context(context_id)


@app.action("feedback_negative")
def negative_feedback(ack, body, client):
    ack()

    context_id = body["actions"][0]["value"]
    ctx = get_context(context_id)

    if not ctx:
        logger.warning(f"Negative feedback action but context {context_id} not found (may have expired)")

        try:
            client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text="Thanks for your feedback.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Thanks for letting us know. Unfortunately this feedback session has expired."
                            )
                        }
                    }
                ]
            )
        except Exception:
            pass
        return

    # Open the feedback modal - trigger_id is valid for 3 seconds - must happen immediately after ack()
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "feedback_modal",
                "private_metadata": context_id,
                "title": {
                    "type": "plain_text",
                    "text": "Help me improve",
                    "emoji": True
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Submit feedback"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel"
                },
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "I'm sorry my answer wasn't helpful. "
                                "Please let me know what was missing or incorrect - your feedback is used to improve future responses."
                            )
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "feedback_input",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "feedback_text",
                            "multiline": True,
                            "min_length": 10,
                            "max_length": 1000,
                            "placeholder": {
                                "type": "plain_text",
                                "text": (
                                    "e.g. The steps were incomplete, the information was outdated, the answer didn't match my issue..."
                                )
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "What was wrong with the answer?"
                        }
                    }
                ]
            }
        )

    except Exception as e:
        logger.error(f"Failed to open feedback modal: {e}", exc_info=True)
        return

    # Update the survey message to indicate the modal is open
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Thanks for the feedback!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Thank you for the feeback, it helps a lot!"
                        )
                    }
                }
            ]
        )

    except Exception as e:
        logger.error(f"Failed to update survey message: {e}", exc_info=True)


@app.view("feedback_modal")
def feedback_modal(ack, body, client, view):
    user_feedback = (view["state"]["values"]["feedback_input"]["feedback_text"].get("value", "") or "").strip()

    if not user_feedback:
        ack(
            response_action="errors",
            errors={
                "feedback_input": (
                    "Please enter some feedback before submitting."
                )
            }
        )
        return

    # Show confirmation screen to the user inside the modal
    ack(
        response_action="update",
        view={
            "type": "modal",
            "title": {
                "type": "plain_text",
                "text": "Thank you!",
                "emoji": True
            },
            "close": {
                "type": "plain_text",
                "text": "Close"
            },
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":white_check_mark: *Your feedback has been received.*\n\n"
                            "Thank you for helping improve KeyWatchBot. Your response will be used to provide more accurate answers in the future."
                        )
                    }
                }
            ]
        }
    )

    # Everything below runs after ack()
    context_id = view["private_metadata"]
    ctx = get_context(context_id)

    if not ctx:
        logger.warning(
            f"Feedback modal submitted but context "
            f"{context_id} expired - sending partial notification"
        )

        # Build a minimal context from what we have in the body
        ctx = {
            "user_id": body["user"]["id"],
            "channel_id": "unknown",
            "question": "unknown (context expired)",
            "answer": "unknown (context expired)",
            "sources": {},
            "history": []
        }

    # Store the negative feedback
    feedback_store.add_negative(
        user_id=ctx["user_id"],
        question=ctx["question"],
        answer=ctx["answer"],
        sources=list(ctx["sources"].keys()),
        user_feedback=user_feedback,
        history=ctx.get("history", [])
    )

    clear_context(context_id)

    # Notify all admins via DM
    notify_neg_feedback(client=client, ctx=ctx, user_feedback=user_feedback)

    logger.info(f"Negative feedback processed for user {ctx['user_id']}")


# Slack commands
@app.command("/reload")
def index_reload(ack, say, command):
    ack()

    if command["user_id"] not in ADMIN_IDS:
        say(":lock: Sorry, only admins can reload the knowledge base.")
        return

    say(":arrows_counterclockwise: Reloading knowledge base - this may take a moment...")

    def do_ingest():
        try:
            result = run_ingest()
            say(result)
        except Exception as e:
            logger.error(f"Ingest failed in /reload: {e}", exc_info=True)
            say(":x: Reload failed. Please check the server logs.")

    threading.Thread(target=do_ingest, daemon=True).start()


@app.command("/clear-chat")
def clear_chat(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    chat_history.clear_history(user_id, channel_id)
    cancel_timer(user_id)
    say(":broom: Our conversation history has been cleared.")


@app.command("/chat-stats")
def chat_stats(ack, say, command):
    ack()
    if command["user_id"] not in ADMIN_IDS:
        say(":lock: Sorry, only admins can view feedback stats.")
        return
    stats = feedback_store.get_stats()
    say(
        f":bar_chart: *KeyWatchBot Feedback Stats*\n"
        f"• 👍 Positive: *{stats['positive']}*\n"
        f"• 👎 Negative: *{stats['negative']}*"
    )


# Entry Point
if __name__ == "__main__":
    handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    handler.start()
