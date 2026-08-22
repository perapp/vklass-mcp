"""Command-line entrypoint."""

from __future__ import annotations

import logging

import uvicorn

from vklass_mcp.app import create_app
from vklass_mcp.config import Settings


def main() -> None:
    settings = Settings()
    settings.validate_security()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=False,
        server_header=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
