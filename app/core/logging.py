import logging
import re
import traceback

_CREDENTIAL_IN_URL = re.compile(r"://([^:/?#\s]+):([^@\s]+)@")
_OPENAI_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_GEMINI_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def configure_logging() -> None:
    """Configure process logging once.

    Uvicorn installs its own handlers when it starts the server. basicConfig
    then leaves that setup in place.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def log_exception(logger: logging.Logger, exc: BaseException) -> None:
    """Log a traceback with URL passwords redacted."""
    rendered = "".join(traceback.format_exception(exc))
    redacted = _CREDENTIAL_IN_URL.sub(r"://\1:***@", rendered)
    redacted = _OPENAI_KEY.sub("sk-***", redacted)
    redacted = _GEMINI_KEY.sub("AIza***", redacted)
    logger.error("unhandled error\n%s", redacted)
