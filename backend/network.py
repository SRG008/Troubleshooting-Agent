"""Outbound network setup, applied once when the backend starts."""

import logging
import os

import truststore

from backend.config import settings

logger = logging.getLogger(__name__)
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def configure_outbound_network() -> None:
    """Routes outbound HTTP through the configured proxy and trusts the OS certificate store.

    Environment variables are used so every client picks them up: httpx, the MCP SDK, and git subprocesses.
    """
    if settings.https_proxy:
        for name in ("HTTPS_PROXY", "HTTP_PROXY"):
            os.environ.setdefault(name, settings.https_proxy)
        no_proxy = [host for host in os.environ.get("NO_PROXY", "").split(",") if host.strip()]
        os.environ["NO_PROXY"] = ",".join(dict.fromkeys([*no_proxy, *_LOCAL_HOSTS]))
        logger.info("Outbound HTTP uses the proxy %s", settings.https_proxy)
    if settings.use_system_certificates:
        truststore.inject_into_ssl()
