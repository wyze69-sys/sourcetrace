"""ASGI application entry point."""

from sourcetrace.api.app import create_app

app = create_app()
