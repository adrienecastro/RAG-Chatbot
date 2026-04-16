import logging
from logging.handlers import RotatingFileHandler

# Logs errors to /opt/KeyWatchBot/error.log and rotates logs up to 20MB
def error_logging():
    log_path = "/opt/KeyWatchBot/logs/error.log"

    # Root logger - captures everything
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Console handler - INFO and above goes to journalctl
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console_handler.setFormatter(console_formatter)

    # File handler - WARNING and above goes to error.log
    # RotatingFileHandler keeps the log from growing larger than 20 MB
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=4)
    file_handler.setLevel(logging.WARNING)
    file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Suppress noisy third-party loggers in the error log
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)

