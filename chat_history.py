import time
import logging
import threading
from collections import defaultdict

logger = logging.getLogger(__name__)


# Stores per-user per-channel conversation histories as a dict
class chatHistory:
    def __init__(self, max_turns: int = 10, ttl_seconds: int = 3600):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._histories = defaultdict(list)
        self._last_active = {}
        self._lock = threading.Lock()

        # Background thread to clean up expired histories
        self._cleanup_thread = threading.Thread(target=self.cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def user_key(self, user_id: str, channel_id: str) -> str:
        return f"{user_id}:{channel_id}"

    def store_user_message(self, user_id: str, channel_id: str, content: str):
        key = self.user_key(user_id, channel_id)
        with self._lock:
            self._histories[key].append({
                "role": "user",
                "content": content
            })
            self._last_active[key] = time.time()
            self._trim(key)

    def store_bot_message(self, user_id: str, channel_id: str, content: str):
        key = self.user_key(user_id, channel_id)
        with self._lock:
            self._histories[key].append({
                "role": "assistant",
                "content": content
            })
            self._last_active[key] = time.time()
            self._trim(key)

    def get_history(self, user_id: str, channel_id: str ) -> list:
        key = self.user_key(user_id, channel_id)
        with self._lock:
            return list(self._histories[key])

    def clear_history(self, user_id: str, channel_id: str):
        key = self.user_key(user_id, channel_id)
        with self._lock:
            self._histories.pop(key, None)
            self._last_active.pop(key, None)

    # Keep only the last max_turns * 2 messages (each turn = 1 user message + 1 assistant message). Called within lock.
    def _trim(self, key: str):
        max_messages = self.max_turns * 2
        if len(self._histories[key]) > max_messages:
            self._histories[key] = self._histories[key][-max_messages:]

    # Delete expired chat histories
    def cleanup_loop(self):
        while True:
            time.sleep(300)   # check every 5 minutes
            now = time.time()
            with self._lock:
                expired = [key for key, last in self._last_active.items() if now - last > self.ttl_seconds]
                for key in expired:
                    self._histories.pop(key, None)
                    self._last_active.pop(key, None)
                if expired:
                    logger.info(f"Cleared {len(expired)} expired chat histories")
