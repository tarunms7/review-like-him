"""FastAPI webhook server for review-bot."""

from review_bot.server.app import create_app

__all__ = ["create_app"]
