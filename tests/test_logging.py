import logging

from app.core.logging import log_exception


def test_log_exception_redacts_url_passwords(caplog: logging.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction")

    with caplog.at_level(logging.ERROR, logger="test.redaction"):
        try:
            raise RuntimeError("postgresql://chatbot:secret-password@localhost/chatbot")
        except RuntimeError as exc:
            log_exception(logger, exc)

    rendered = caplog.text
    assert "secret-password" not in rendered
    assert "chatbot:***@" in rendered


def test_log_exception_redacts_openai_keys(caplog: logging.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction.openai")

    with caplog.at_level(logging.ERROR, logger="test.redaction.openai"):
        try:
            raise RuntimeError("rejected key sk-test-secret-value")
        except RuntimeError as exc:
            log_exception(logger, exc)

    assert "sk-test-secret-value" not in caplog.text
    assert "sk-***" in caplog.text


def test_log_exception_redacts_gemini_keys(caplog: logging.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction.gemini")

    with caplog.at_level(logging.ERROR, logger="test.redaction.gemini"):
        try:
            raise RuntimeError("rejected key AIzaSyTestSecretValue1234567890")
        except RuntimeError as exc:
            log_exception(logger, exc)

    assert "AIzaSyTestSecretValue1234567890" not in caplog.text
    assert "AIza***" in caplog.text
