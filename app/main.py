import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import create_engine_and_factory
from app.services.ai import build_provider
from app.services.ai.provider import AIProvider

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    ai_provider: AIProvider | None = None,
) -> FastAPI:
    """Build the API application."""
    configure_logging()
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_factory(resolved_settings.database_url)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.ai_provider = ai_provider or build_provider(resolved_settings)
        logger.info(
            "api started environment=%s provider=%s",
            resolved_settings.environment,
            resolved_settings.ai_provider,
        )
        yield
        await engine.dispose()

    app = FastAPI(title="AI Chatbot API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-CSRF-Token"],
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
