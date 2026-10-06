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
