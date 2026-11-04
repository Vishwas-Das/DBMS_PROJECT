"""ASGI entry point: `uvicorn lae.api.main:app --port 8000`.

Reads configuration from LAE_* environment variables / .env (see .env.example).
"""

from lae.api.app import create_app
from lae.config import get_settings
from lae.services.impl import build_services
from lae.services.runtime import configure_logging

configure_logging()
_settings = get_settings()
app = create_app(build_services, api_token=_settings.api_token)
