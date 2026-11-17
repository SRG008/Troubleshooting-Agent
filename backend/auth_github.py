import json
import logging
import secrets
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from backend.config import settings

logger = logging.getLogger(__name__)

GITHUB_WEB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
# read:org verifies organization membership; repo includes private repositories the user can access.
GITHUB_OAUTH_SCOPES = "read:org repo"
GITHUB_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubConnectionError(Exception):
    """Carries a user-facing message when the GitHub connection cannot be completed."""


class GitHubRepository(BaseModel):
    name: str
    full_name: str
    private: bool
    html_url: str


class GitHubStatus(BaseModel):
    connected: bool = False
    login: Optional[str] = None
    organization: Optional[str] = None
    repositories: List[GitHubRepository] = Field(default_factory=list)
    selected_repositories: List[str] = Field(default_factory=list)
    local_folders: Dict[str, str] = Field(default_factory=dict)


class GitHubSelectionRequest(BaseModel):
    repositories: List[str] = Field(default_factory=list)


@dataclass
class _GitHubConnection:
    # The token stays server-side and is never returned by the status endpoint.
    access_token: str
    login: str
    user_id: int = 0
    repositories: List[GitHubRepository] = field(default_factory=list)
    selected: List[str] = field(default_factory=list)
    # full_name -> local checkout path that the user or agent prepared for this repo.
    checkouts: Dict[str, str] = field(default_factory=dict)


# In-memory stores (Replace with Redis in multi-worker production), like auth_jira.py.
GITHUB_SESSION_STORE: Dict[str, _GitHubConnection] = {}
# full_name -> folder where the repository was cloned on this machine. Kept across chat sessions.
GITHUB_CLONE_REGISTRY: Dict[str, str] = {}
PENDING_OAUTH_STATES: Dict[str, str] = {}  # OAuth state -> session_id

# Connections are also saved encrypted on disk so backend restarts and auto-reloads keep them.
GITHUB_STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "github"
GITHUB_KEY_FILE = GITHUB_STATE_DIR / "store.key"
GITHUB_STORE_FILE = GITHUB_STATE_DIR / "connections.enc"


def _fernet() -> Fernet:
    GITHUB_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not GITHUB_KEY_FILE.exists():
        GITHUB_KEY_FILE.write_bytes(Fernet.generate_key())
    return Fernet(GITHUB_KEY_FILE.read_bytes())


def save_github_store() -> None:
    payload = {
        "sessions": {
            session_id: {
                "access_token": connection.access_token,
                "login": connection.login,
                "user_id": connection.user_id,
                "repositories": [repo.model_dump() for repo in connection.repositories],
                "selected": connection.selected,
                "checkouts": connection.checkouts,
            }
            for session_id, connection in GITHUB_SESSION_STORE.items()
        },
        "clones": GITHUB_CLONE_REGISTRY,
    }
    encrypted = _fernet().encrypt(json.dumps(payload).encode("utf-8"))
    temp_file = GITHUB_STORE_FILE.with_suffix(".tmp")
    temp_file.write_bytes(encrypted)
    temp_file.replace(GITHUB_STORE_FILE)


def _load_github_store() -> None:
    if not GITHUB_STORE_FILE.exists():
        return
    try:
        payload = json.loads(_fernet().decrypt(GITHUB_STORE_FILE.read_bytes()))
    except (InvalidToken, ValueError, OSError):
        logger.warning("Saved GitHub connections could not be read; users must connect GitHub again.")
        return
    # Older files hold the sessions at the top level, without the "sessions" and "clones" keys.
    sessions = payload["sessions"] if "sessions" in payload else payload
    GITHUB_CLONE_REGISTRY.update(payload.get("clones", {}))
    for session_id, data in sessions.items():
        GITHUB_SESSION_STORE[session_id] = _GitHubConnection(
            access_token=data["access_token"],
            login=data["login"],
            user_id=data.get("user_id", 0),
            repositories=[GitHubRepository(**repo) for repo in data.get("repositories", [])],
            selected=data.get("selected", []),
            checkouts=data.get("checkouts", {}),
        )


_load_github_store()


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, headers=GITHUB_API_HEADERS)


def _require_oauth_settings() -> None:
    missing = [
        name
        for name, value in (
            ("GITHUB_CLIENT_ID", settings.github_client_id),
            ("GITHUB_CLIENT_SECRET", settings.github_client_secret),
            ("GITHUB_ORGANIZATION", settings.github_organization),
        )
        if not value
    ]
    if missing:
        raise GitHubConnectionError(f"{', '.join(missing)} must be set in .env for GitHub login.")


def get_github_status(session_id: str) -> GitHubStatus:
    connection = GITHUB_SESSION_STORE.get(session_id)
    if connection is None:
        return GitHubStatus()
    return GitHubStatus(
        connected=True,
        login=connection.login,
        organization=settings.github_organization,
        repositories=connection.repositories,
        selected_repositories=list(connection.selected),
        local_folders=dict(connection.checkouts),
    )


def get_github_connection(session_id: str) -> Optional[_GitHubConnection]:
    return GITHUB_SESSION_STORE.get(session_id)


def set_selected_repositories(session_id: str, full_names: List[str]) -> GitHubStatus:
    connection = GITHUB_SESSION_STORE.get(session_id)
    if connection is None:
        raise GitHubConnectionError("Connect GitHub first.")
    available = {repo.full_name for repo in connection.repositories}
    unknown = [name for name in full_names if name not in available]
    if unknown:
        raise GitHubConnectionError(f"Not accessible to your account: {', '.join(unknown)}")
    connection.selected = list(dict.fromkeys(full_names))
    save_github_store()
    return get_github_status(session_id)


def disconnect_github(session_id: str) -> GitHubStatus:
    GITHUB_SESSION_STORE.pop(session_id, None)
    save_github_store()
    return GitHubStatus()


def build_github_oauth_url(session_id: str) -> str:
    """Builds the GitHub OAuth authorization URL with a random, single-use state bound to the session."""
    _require_oauth_settings()
    state = secrets.token_urlsafe(32)
    PENDING_OAUTH_STATES[state] = session_id
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": GITHUB_OAUTH_SCOPES,
        "state": state,
    }
    return f"{GITHUB_WEB_URL}/login/oauth/authorize?{urllib.parse.urlencode(params)}"


def discard_oauth_state(state: str) -> None:
    PENDING_OAUTH_STATES.pop(state, None)


async def complete_github_connection(code: str, state: str) -> str:
    """Exchanges the OAuth code, checks organization membership, and stores the user's accessible repositories."""
    session_id = PENDING_OAUTH_STATES.pop(state, None)
    if session_id is None:
        raise GitHubConnectionError("The sign-in request expired or was already used. Click Connect GitHub again.")

    async with _http_client() as client:
        token = await _exchange_code(client, code)
        login, user_id = await _fetch_user(client, token)
        await _require_membership(client, token, login)
        repositories = await _fetch_organization_repositories(client, token)

    GITHUB_SESSION_STORE[session_id] = _GitHubConnection(
        access_token=token, login=login, user_id=user_id, repositories=repositories
    )
    save_github_store()
    return session_id


async def _exchange_code(client: httpx.AsyncClient, code: str) -> str:
    resp = await client.post(
        f"{GITHUB_WEB_URL}/login/oauth/access_token",
        headers={"Accept": "application/json"},
        json={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": settings.github_redirect_uri,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GitHubConnectionError(body.get("error_description") or "GitHub did not return an access token.")
    return token


async def _github_get(client: httpx.AsyncClient, token: str, path: str, params: Optional[dict] = None) -> httpx.Response:
    return await client.get(f"{GITHUB_API_URL}{path}", params=params, headers={"Authorization": f"Bearer {token}"})


def drop_rejected_connection(session_id: str) -> None:
    """Forgets a session whose GitHub token was rejected (revoked or expired). Saved clone folders are kept."""
    if GITHUB_SESSION_STORE.pop(session_id, None) is not None:
        save_github_store()


def _describe_api_error(resp: httpx.Response) -> str:
    if resp.status_code == 401:
        return "GitHub rejected the saved login. Connect GitHub again from the sidebar and try again."
    if resp.status_code == 403:
        return (
            "GitHub denied access. If the organization enforces SAML SSO, authorize this OAuth app "
            "for the organization in your GitHub settings and try again."
        )
    return f"GitHub API returned HTTP {resp.status_code}."


async def _fetch_user(client: httpx.AsyncClient, token: str) -> Tuple[str, int]:
    resp = await _github_get(client, token, "/user")
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    body = resp.json()
    return body["login"], int(body.get("id") or 0)


async def _require_membership(client: httpx.AsyncClient, token: str, login: str) -> None:
    org = settings.github_organization
    resp = await _github_get(client, token, f"/user/memberships/orgs/{org}")
    if resp.status_code == 404:
        raise GitHubConnectionError(f"{login} is not a member of the {org} GitHub organization.")
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    if resp.json().get("state") != "active":
        raise GitHubConnectionError(f"Accept your invitation to the {org} GitHub organization first.")


async def _fetch_organization_repositories(client: httpx.AsyncClient, token: str) -> List[GitHubRepository]:
    """Repositories in the configured organization that this user can access (direct, team, or collaborator)."""
    org = settings.github_organization.lower()
    repositories: List[GitHubRepository] = []
    page = 1
    while True:
        resp = await _github_get(
            client,
            token,
            "/user/repos",
            params={
                "affiliation": "organization_member,collaborator",
                "sort": "full_name",
                "per_page": 100,
                "page": page,
            },
        )
        if resp.status_code != 200:
            raise GitHubConnectionError(_describe_api_error(resp))
        batch = resp.json()
        for item in batch:
            if item["owner"]["login"].lower() == org:
                repositories.append(
                    GitHubRepository(
                        name=item["name"],
                        full_name=item["full_name"],
                        private=item["private"],
                        html_url=item["html_url"],
                    )
                )
        if len(batch) < 100:
            return repositories
        page += 1


def require_selected_repository(session_id: str, full_name: str) -> _GitHubConnection:
    """Returns the session's connection when the repository is selected for the assistant, otherwise raises."""
    connection = GITHUB_SESSION_STORE.get(session_id)
    if connection is None:
        raise GitHubConnectionError("GitHub is not connected.")
    if full_name not in connection.selected:
        raise GitHubConnectionError(f"{full_name} is not selected in the sidebar.")
    return connection


async def list_branch_names(session_id: str, full_name: str) -> List[str]:
    connection = require_selected_repository(session_id, full_name)
    async with _http_client() as client:
        resp = await _github_get(
            client, connection.access_token, f"/repos/{full_name}/branches", params={"per_page": 100}
        )
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    return [branch["name"] for branch in resp.json()]


async def list_pull_requests(session_id: str, full_name: str, state: str = "open") -> List[Dict]:
    connection = require_selected_repository(session_id, full_name)
    async with _http_client() as client:
        resp = await _github_get(
            client,
            connection.access_token,
            f"/repos/{full_name}/pulls",
            params={"state": state, "per_page": 30, "sort": "updated", "direction": "desc"},
        )
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "state": pr["state"],
            "author": (pr.get("user") or {}).get("login", ""),
            "head": pr["head"]["ref"],
            "base": pr["base"]["ref"],
            "created_at": pr["created_at"],
            "url": pr["html_url"],
        }
        for pr in resp.json()
    ]


async def open_pull_request(session_id: str, full_name: str, head: str, base: str, title: str, body: str) -> Dict:
    connection = require_selected_repository(session_id, full_name)
    async with _http_client() as client:
        resp = await client.post(
            f"{GITHUB_API_URL}/repos/{full_name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
            headers={"Authorization": f"Bearer {connection.access_token}"},
        )
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json()
            reasons = [detail.get("message") or ""] + [e.get("message", "") for e in detail.get("errors", [])]
            message = "; ".join(r for r in reasons if r)
        except ValueError:
            message = ""
        raise GitHubConnectionError(f"GitHub did not open the pull request: {message or resp.status_code}")
    created = resp.json()
    return {"number": created["number"], "url": created["html_url"]}
