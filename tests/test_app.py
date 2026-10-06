from app.main import create_app


def test_create_app_exposes_service_metadata() -> None:
    app = create_app()

    assert app.title == "AI Chatbot API"
    assert app.version == "0.1.0"
