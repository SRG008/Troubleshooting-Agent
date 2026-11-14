import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from backend.config import settings

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


@dataclass
class _GitHubConnection:
    # The token stays server-side and is never returned by the status endpoint.
    access_token: str
    login: str
    repositories: List[GitHubRepository] = field(default_factory=list)


# In-memory stores (Replace with Redis in multi-worker production), like auth_jira.py.
GITHUB_SESSION_STORE: Dict[str, _GitHubConnection] = {}
PENDING_OAUTH_STATES: Dict[str, str] = {}  # OAuth state -> session_id


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
    )


def disconnect_github(session_id: str) -> GitHubStatus:
    GITHUB_SESSION_STORE.pop(session_id, None)
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
        login = await _fetch_login(client, token)
        await _require_membership(client, token, login)
        repositories = await _fetch_organization_repositories(client, token)

    GITHUB_SESSION_STORE[session_id] = _GitHubConnection(access_token=token, login=login, repositories=repositories)
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


def _describe_api_error(resp: httpx.Response) -> str:
    if resp.status_code == 403:
        return (
            "GitHub denied access. If the organization enforces SAML SSO, authorize this OAuth app "
            "for the organization in your GitHub settings and try again."
        )
    return f"GitHub API returned HTTP {resp.status_code}."


async def _fetch_login(client: httpx.AsyncClient, token: str) -> str:
    resp = await _github_get(client, token, "/user")
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    return resp.json()["login"]


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
