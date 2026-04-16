import logging, os, time, threading, uuid
from dotenv import load_dotenv
from slack_bolt import App

load_dotenv()
logger = logging.getLogger(__name__)


# Variables
SURVEY_DELAY = 900   # 15 minutes
PDF_MIN_IMAGE_SCORE = 0.45
SLACKBOT_ADMINS = [os.getenv("SLACKBOT_ADMIN_IDS")]
app = App(token=os.getenv("SLACK_BOT_TOKEN"))


# Per-user timer: {user_id: threading.Timer}
survey_timers: dict = {}
survey_timers_lock = threading.Lock()

# Stores the conversation context needed for feedback storage and admin notification. Cleaned up after use or after 2 hours.
pending_contexts: dict = {}
pending_contexts_lock = threading.Lock()


# Slack Formatters
def slack_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

def slack_link(url: str, label: str) -> str:
    return f"<{url}|{slack_escape(label)}>"


# Store survey context as a UUID reference
def save_context(user_id: str, channel_id: str, question: str, answer: str, sources: dict, history: list) -> str:
    context_id = str(uuid.uuid4())
    with pending_contexts_lock:
        pending_contexts[context_id] = {
            "user_id": user_id,
            "channel_id": channel_id,
            "question": question,
            "answer": answer,
            "sources": sources,
            "history": list(history) if history else [],
            "stored_at": time.time()
        }
    return context_id


def get_context(context_id: str) -> dict:
    with pending_contexts_lock:
        return pending_contexts.get(context_id)


def clear_context(context_id: str):
    with pending_contexts_lock:
        pending_contexts.pop(context_id, None)


# Remove contexts older than 2 hours
def clear_expired_context():
    while True:
        time.sleep(600)   # check every 10 minutes
        cutoff = time.time() - 7200   # 2 hours
        with pending_contexts_lock:
            expired = [cid for cid, ctx in pending_contexts.items() if ctx.get("stored_at", 0) < cutoff]
            for cid in expired:
                pending_contexts.pop(cid, None)
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired survey contexts")

threading.Thread(target=clear_expired_context, daemon=True, name="context-cleanup").start()


# Cancels survey timer for this user.
def cancel_timer(user_id: str):
    with survey_timers_lock:
        timer = survey_timers.pop(user_id, None)
    if timer:
        timer.cancel()


# Cancels timer and reschedules survey
def schedule_survey(user_id: str, channel_id: str, question: str, answer: str, sources: dict, history: list):
    cancel_timer(user_id)

    context_id = save_context(user_id, channel_id, question, answer, sources, history)

    def send_survey():
        try:
            app.client.chat_postMessage(
                channel=channel_id,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Was *KeyWatchBot*'s answer helpful? "
                                "Your feedback helps improve future "
                                "responses. :speech_balloon:"
                            )
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "👍  Yes, it was helpful",
                                    "emoji": True
                                },
                                "style": "primary",
                                "action_id": "feedback_positive",
                                "value": context_id
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "👎  No, it wasn't helpful",
                                    "emoji": True
                                },
                                "style": "danger",
                                "action_id": "feedback_negative",
                                "value": context_id
                            }
                        ]
                    }
                ],
                text="Was KeyWatchBot's answer helpful?"
            )
            logger.info(f"Survey sent to user {user_id} in channel {channel_id}")

        except Exception as e:
            logger.error(f"Failed to send survey to {user_id}: {e}", exc_info=True)

        finally:
            with survey_timers_lock:
                survey_timers.pop(user_id, None)

    timer = threading.Timer(SURVEY_DELAY, send_survey)
    timer.daemon = True

    with survey_timers_lock:
        survey_timers[user_id] = timer

    timer.start()
    logger.info(f"Survey scheduled for {user_id} in {SURVEY_DELAY // 60} minutes")


#Slackbot Admin notifications
def slack_truncate(text: str, limit: int = 2900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n_[truncated]_"

# Send a detailed DM to slackbot admin(s) with the full conversation context.
def notify_neg_feedback(client, ctx: dict, user_feedback: str):
    if not SLACKBOT_ADMINS:
        logger.warning("No admin IDs configured - cannot send feedback notification")
        return

    # Format conversation history
    history_text = "_No prior history_"
    if ctx.get("history"):
        lines = []
        for entry in ctx["history"]:
            role = "User" if entry["role"] == "user" else "Bot"
            lines.append(f"*{role}:* {slack_escape(entry['content'])}")
        history_text = "\n".join(lines)

    # Format sources
    source_lines = []
    for name, url in ctx.get("sources", {}).items():
        if url:
            source_lines.append(f"• {slack_link(url, name)}")
        else:
            source_lines.append(f"• {slack_escape(name)}")
    sources_text = ("\n".join(source_lines) if source_lines else "- No sources -")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⚠️ Negative Feedback Received",
                "emoji": True
            }
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*User:* <@{ctx['user_id']}>"
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Channel:* <#{ctx['channel_id']}>"
                }
            ]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Question asked:*\n"
                    f"{slack_truncate(slack_escape(ctx['question']))}"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Bot's response:*\n"
                    f"{slack_truncate(slack_escape(ctx['answer']))}"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Sources referenced:*\n{sources_text}"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Conversation history:*\n"
                    f"{slack_truncate(history_text)}"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*User's feedback:*\n - {slack_escape(user_feedback)}"
                )
            }
        }
    ]

    for admin_id in SLACKBOT_ADMINS:
        try:
            dm = client.conversations_open(users=admin_id)
            dm_channel = dm["channel"]["id"]
            client.chat_postMessage(
                channel=dm_channel,
                text="⚠️ A user left negative feedback on a bot response.",
                blocks=blocks
            )
            logger.info(f"Sent negative feedback notification to admin {admin_id}")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}", exc_info=True)

