"""Atlassian sign-in for the Rovo MCP server.

Two modes:
- OAuth 2.1: the backend registers itself with Atlassian (dynamic client registration), then each user
  signs in with PKCE and a random single-use state. Tokens are refreshed automatically.
- API token: a personal API token with the agent-interface scopes, sent as Basic auth. The organization
  admin must enable API-token authentication for the Rovo MCP server.

Tokens stay on the server. They are saved encrypted on disk and never returned by the status endpoint.
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from backend.config import settings

logger = logging.getLogger(__name__)

OAUTH_MODE = "oauth"
API_TOKEN_MODE = "api_token"
CLIENT_NAME = "Troubleshooting Agent"
PENDING_LOGIN_TTL_SECONDS = 600.0
REFRESH_MARGIN_SECONDS = 60.0
# Atlassian's consent form always submits these scopes too. A client that did not register and request
# them gets "invalid_request: Incorrect request parameters" when the user clicks Accept.
CONSENT_SCOPES = ("read:me", "read:account", "offline_access")

ATLASSIAN_STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "atlassian"
ATLASSIAN_KEY_FILE = ATLASSIAN_STATE_DIR / "store.key"
ATLASSIAN_STORE_FILE = ATLASSIAN_STATE_DIR / "connections.enc"


class AtlassianAuthError(Exception):
    """Carries a user-facing message when the Atlassian sign-in cannot be completed or used."""


class AtlassianSite(BaseModel):
    id: str = Field(..., description="Atlassian cloud ID")
    name: str
    url: str = ""


class AtlassianStatus(BaseModel):
    connected: bool = False
    auth_mode: Optional[str] = None
    account: Optional[str] = None
    cloud_id: Optional[str] = None
    site_name: Optional[str] = None
    site_url: Optional[str] = None
    sites: List[AtlassianSite] = Field(default_factory=list)


class APITokenConnectRequest(BaseModel):
    site_url: str = Field(..., min_length=3, examples=["https://your-domain.atlassian.net"])
    email: str = Field(..., min_length=3, examples=["engineer@company.com"])
    api_token: str = Field(..., min_length=1, description="Atlassian API token with the agent-interface scopes")


# API tokens are not bound to a site, so the user names it. Only Atlassian Cloud hosts are contacted.
JIRA_CLOUD_HOST_SUFFIXES = (".atlassian.net", ".jira.com")


def normalize_site_url(value: str) -> str:
    raw = value.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(JIRA_CLOUD_HOST_SUFFIXES) or parts.port:
        raise AtlassianAuthError("Enter your Jira Cloud site URL, for example https://your-domain.atlassian.net.")
    return f"https://{host}"


@dataclass
class _Connection:
    auth_mode: str
    access_token: str
    refresh_token: str = ""
    # Epoch seconds; 0 means the credential does not expire (API token).
    expires_at: float = 0.0
    email: str = ""
    account: str = ""
    sites: List[AtlassianSite] = field(default_factory=list)
    cloud_id: str = ""


@dataclass
class _PendingLogin:
    session_id: str
    code_verifier: str
    created: float


# In-memory stores (Replace with Redis in multi-worker production), like auth_github.py.
ATLASSIAN_SESSION_STORE: Dict[str, _Connection] = {}
PENDING_LOGINS: Dict[str, _PendingLogin] = {}  # OAuth state -> pending login
# The dynamic client registration, reused for every user while the redirect URI stays the same.
_registration: Dict[str, str] = {}
_server_metadata: Dict[str, Any] = {}
_refresh_locks: Dict[str, asyncio.Lock] = {}


def _fernet() -> Fernet:
    ATLASSIAN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not ATLASSIAN_KEY_FILE.exists():
        ATLASSIAN_KEY_FILE.write_bytes(Fernet.generate_key())
    return Fernet(ATLASSIAN_KEY_FILE.read_bytes())


def save_atlassian_store() -> None:
    payload = {
        "registration": _registration,
        "sessions": {
            session_id: {
                "auth_mode": c.auth_mode,
                "access_token": c.access_token,
                "refresh_token": c.refresh_token,
                "expires_at": c.expires_at,
                "email": c.email,
                "account": c.account,
                "sites": [site.model_dump() for site in c.sites],
                "cloud_id": c.cloud_id,
            }
            for session_id, c in ATLASSIAN_SESSION_STORE.items()
        },
    }
    encrypted = _fernet().encrypt(json.dumps(payload).encode("utf-8"))
    temp_file = ATLASSIAN_STORE_FILE.with_suffix(".tmp")
    temp_file.write_bytes(encrypted)
    temp_file.replace(ATLASSIAN_STORE_FILE)


def _load_atlassian_store() -> None:
    if not ATLASSIAN_STORE_FILE.exists():
        return
    try:
        payload = json.loads(_fernet().decrypt(ATLASSIAN_STORE_FILE.read_bytes()))
    except (InvalidToken, ValueError, OSError):
        logger.warning("Saved Atlassian connections could not be read; users must connect Jira again.")
        return
    _registration.update(payload.get("registration", {}))
    for session_id, data in payload.get("sessions", {}).items():
        ATLASSIAN_SESSION_STORE[session_id] = _Connection(
            auth_mode=data["auth_mode"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=float(data.get("expires_at") or 0.0),
            email=data.get("email", ""),
            account=data.get("account", ""),
            sites=[AtlassianSite(**site) for site in data.get("sites", [])],
            cloud_id=data.get("cloud_id", ""),
        )


_load_atlassian_store()


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, headers={"Accept": "application/json"})


def _well_known(url: str, suffix: str) -> str:
    """RFC 8414 / RFC 9728 location: the well-known segment goes between the host and the path."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/.well-known/{suffix}{parts.path.rstrip('/')}"


async def _metadata(client: httpx.AsyncClient) -> Dict[str, Any]:
    """The MCP server's authorization server metadata, discovered from its protected resource metadata."""
    if _server_metadata.get("mcp_url") == settings.atlassian_mcp_url:
        return _server_metadata
    resource_resp = await client.get(_well_known(settings.atlassian_mcp_url, "oauth-protected-resource"))
    resource_resp.raise_for_status()
    resource = resource_resp.json()
    issuer = resource["authorization_servers"][0]
    server_resp = await client.get(_well_known(issuer, "oauth-authorization-server"))
    server_resp.raise_for_status()
    metadata = server_resp.json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not metadata.get(key):
            raise AtlassianAuthError(f"The Atlassian authorization server does not publish {key}.")
    _server_metadata.clear()
    _server_metadata.update(metadata, mcp_url=settings.atlassian_mcp_url, resource=resource.get("resource") or settings.atlassian_mcp_url)
    return _server_metadata


def _requested_scopes() -> str:
    return " ".join(dict.fromkeys([*settings.atlassian_mcp_scopes.split(), *CONSENT_SCOPES]))


async def _client_id(client: httpx.AsyncClient, metadata: Dict[str, Any]) -> str:
    """Registers this backend as a public OAuth client once, and again when the redirect URI or scopes change."""
    scopes = _requested_scopes()
    if (
        _registration.get("client_id")
        and _registration.get("redirect_uri") == settings.jira_redirect_uri
        and _registration.get("registration_endpoint") == metadata["registration_endpoint"]
        and _registration.get("scope") == scopes
    ):
        return _registration["client_id"]
    resp = await client.post(
        metadata["registration_endpoint"],
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [settings.jira_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": scopes,
        },
    )
    if resp.status_code not in (200, 201):
        raise AtlassianAuthError(f"Atlassian refused to register this app (HTTP {resp.status_code}).")
    _registration.clear()
    _registration.update(
        client_id=resp.json()["client_id"],
        redirect_uri=settings.jira_redirect_uri,
        registration_endpoint=metadata["registration_endpoint"],
        scope=scopes,
    )
    save_atlassian_store()
    return _registration["client_id"]


def _prune_pending_logins() -> None:
    now = time.monotonic()
    for state in [s for s, p in PENDING_LOGINS.items() if now - p.created > PENDING_LOGIN_TTL_SECONDS]:
        del PENDING_LOGINS[state]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def build_oauth_url(session_id: str) -> str:
    """The Atlassian consent URL, with PKCE and a random single-use state bound to the session."""
    async with _http_client() as client:
        metadata = await _metadata(client)
        client_id = await _client_id(client, metadata)
    _prune_pending_logins()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    PENDING_LOGINS[state] = _PendingLogin(session_id=session_id, code_verifier=verifier, created=time.monotonic())
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.jira_redirect_uri,
        "scope": _requested_scopes(),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "resource": metadata["resource"],
    }
    return f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


def discard_oauth_state(state: str) -> None:
    PENDING_LOGINS.pop(state, None)


def _store_tokens(connection: _Connection, body: Dict[str, Any]) -> None:
    connection.access_token = body["access_token"]
    # Refresh tokens rotate; keep the previous one if the server did not send a new one.
    connection.refresh_token = body.get("refresh_token") or connection.refresh_token
    expires_in = body.get("expires_in")
    connection.expires_at = time.time() + float(expires_in) if expires_in else 0.0


async def complete_oauth(code: str, state: str) -> str:
    """Exchanges the authorization code for tokens. Returns the session the sign-in belongs to."""
    _prune_pending_logins()
    pending = PENDING_LOGINS.pop(state, None)
    if pending is None:
        raise AtlassianAuthError("The sign-in request expired or was already used. Click Sign in with Atlassian again.")
    async with _http_client() as client:
        metadata = await _metadata(client)
        resp = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.jira_redirect_uri,
                "client_id": _registration.get("client_id", ""),
                "code_verifier": pending.code_verifier,
                "resource": metadata["resource"],
            },
        )
    if resp.status_code != 200 or not resp.json().get("access_token"):
        raise AtlassianAuthError(f"Atlassian did not issue an access token (HTTP {resp.status_code}).")
    connection = _Connection(auth_mode=OAUTH_MODE, access_token="")
    _store_tokens(connection, resp.json())
    ATLASSIAN_SESSION_STORE[pending.session_id] = connection
    save_atlassian_store()
    return pending.session_id


async def connect_api_token(session_id: str, req: APITokenConnectRequest) -> None:
    """Stores an API-token connection for the named site. The caller verifies it against the MCP server.

    The site's cloud ID comes from Atlassian's public tenant lookup; Rovo tools also accept the hostname,
    which is used if the lookup is unavailable.
    """
    site_url = normalize_site_url(req.site_url)
    host = urllib.parse.urlsplit(site_url).hostname or site_url
    cloud_id = host
    try:
        async with _http_client() as client:
            resp = await client.get(f"{site_url}/_edge/tenant_info")
        if resp.status_code == 404:
            raise AtlassianAuthError(f"No Jira Cloud site was found at {site_url}.")
        if resp.status_code == 200 and resp.json().get("cloudId"):
            cloud_id = str(resp.json()["cloudId"])
    except (httpx.HTTPError, ValueError):
        logger.warning("Could not look up the cloud ID of %s; using the hostname instead.", host)
    email = req.email.strip()
    ATLASSIAN_SESSION_STORE[session_id] = _Connection(
        auth_mode=API_TOKEN_MODE,
        access_token=req.api_token.strip(),
        email=email,
        account=email,
        sites=[AtlassianSite(id=cloud_id, name=host, url=site_url)],
        cloud_id=cloud_id,
    )
    save_atlassian_store()


async def _refresh(session_id: str, connection: _Connection) -> None:
    if not connection.refresh_token:
        drop_connection(session_id)
        raise AtlassianAuthError("The Atlassian sign-in expired. Connect Jira again from the sidebar.")
    async with _http_client() as client:
        metadata = await _metadata(client)
        resp = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
                "client_id": _registration.get("client_id", ""),
                "resource": metadata["resource"],
            },
        )
    if resp.status_code != 200 or not resp.json().get("access_token"):
        drop_connection(session_id)
        raise AtlassianAuthError("The Atlassian sign-in expired. Connect Jira again from the sidebar.")
    _store_tokens(connection, resp.json())
    save_atlassian_store()


async def authorization_header(session_id: str, force_refresh: bool = False) -> str:
    """The Authorization header for the session's MCP requests, refreshing an OAuth token when it is about to expire."""
    connection = ATLASSIAN_SESSION_STORE.get(session_id)
    if connection is None:
        raise AtlassianAuthError("Jira is not connected.")
    if connection.auth_mode == API_TOKEN_MODE:
        basic = base64.b64encode(f"{connection.email}:{connection.access_token}".encode("utf-8")).decode("ascii")
        return f"Basic {basic}"
    async with _refresh_locks.setdefault(session_id, asyncio.Lock()):
        expiring = connection.expires_at and time.time() > connection.expires_at - REFRESH_MARGIN_SECONDS
        if force_refresh or expiring:
            await _refresh(session_id, connection)
    return f"Bearer {connection.access_token}"


def can_refresh(session_id: str) -> bool:
    connection = ATLASSIAN_SESSION_STORE.get(session_id)
    return bool(connection and connection.auth_mode == OAUTH_MODE and connection.refresh_token)


def is_connected(session_id: str) -> bool:
    return session_id in ATLASSIAN_SESSION_STORE


def set_workspace(session_id: str, sites: List[AtlassianSite], account: str) -> None:
    """Saves the sites the user can access and keeps the chosen site when it is still available."""
    connection = ATLASSIAN_SESSION_STORE.get(session_id)
    if connection is None:
        return
    connection.sites = sites
    connection.account = account or connection.email
    if connection.cloud_id not in {site.id for site in sites}:
        connection.cloud_id = sites[0].id if sites else ""
    save_atlassian_store()


def select_site(session_id: str, site_id: str) -> AtlassianStatus:
    connection = ATLASSIAN_SESSION_STORE.get(session_id)
    if connection is None:
        raise AtlassianAuthError("Jira is not connected.")
    if site_id not in {site.id for site in connection.sites}:
        raise AtlassianAuthError("That site is not available to your account.")
    connection.cloud_id = site_id
    save_atlassian_store()
    return get_atlassian_status(session_id)


def get_atlassian_status(session_id: str) -> AtlassianStatus:
    connection = ATLASSIAN_SESSION_STORE.get(session_id)
    if connection is None:
        return AtlassianStatus()
    site = next((s for s in connection.sites if s.id == connection.cloud_id), None)
    return AtlassianStatus(
        connected=True,
        auth_mode=connection.auth_mode,
        account=connection.account or connection.email or None,
        cloud_id=connection.cloud_id or None,
        site_name=site.name if site else None,
        site_url=site.url if site else None,
        sites=list(connection.sites),
    )


def drop_connection(session_id: str) -> None:
    if ATLASSIAN_SESSION_STORE.pop(session_id, None) is not None:
        save_atlassian_store()


async def disconnect_atlassian(session_id: str) -> AtlassianStatus:
    """Forgets the session's connection and, for OAuth, revokes its refresh token on a best-effort basis."""
    connection = ATLASSIAN_SESSION_STORE.pop(session_id, None)
    save_atlassian_store()
    revocation_endpoint = _server_metadata.get("revocation_endpoint")
    if connection and connection.auth_mode == OAUTH_MODE and connection.refresh_token and revocation_endpoint:
        try:
            async with _http_client() as client:
                await client.post(
                    revocation_endpoint,
                    data={"token": connection.refresh_token, "client_id": _registration.get("client_id", "")},
                )
        except httpx.HTTPError:
            logger.warning("Revoking the Atlassian refresh token failed; it expires on its own.")
    return AtlassianStatus()
