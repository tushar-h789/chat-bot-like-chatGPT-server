from fastapi import APIRouter

from app.api.v1 import admin, auth, chat, conversations, files, health, messages, usage

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(messages.router)
api_router.include_router(chat.router)
api_router.include_router(usage.router)
api_router.include_router(files.router)
api_router.include_router(admin.router)
