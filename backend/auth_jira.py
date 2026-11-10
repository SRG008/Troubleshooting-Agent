import urllib.parse
import httpx
from typing import Dict
from backend.config import settings
from backend.models import JiraSessionState, JiraSiteResource, PATConnectRequest

# In-memory session store (Replace with Redis in multi-worker production)
SESSION_STORE: Dict[str, JiraSessionState] = {}


def get_or_create_session(session_id: str) -> JiraSessionState:
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = JiraSessionState()
    return SESSION_STORE[session_id]


def disconnect_session(session_id: str) -> JiraSessionState:
    """Resets the Jira session state to disconnected."""
    SESSION_STORE[session_id] = JiraSessionState()
    return SESSION_STORE[session_id]


def build_atlassian_oauth_url(session_id: str) -> str:
    """Constructs the Atlassian 3LO Authorization URL."""
    scopes = "read:jira-work read:jira-user write:jira-work offline_access"
    params = {
        "audience": "api.atlassian.com",
        "client_id": settings.jira_client_id,
        "scope": scopes,
        "redirect_uri": settings.jira_redirect_uri,
        "state": session_id,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"https://auth.atlassian.com/authorize?{urllib.parse.urlencode(params)}"


async def exchange_code_and_resolve_cloud_id(code: str, session_id: str) -> JiraSessionState:
    """Exchanges OAuth code for token and dynamically discovers the company Cloud ID."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
            "https://auth.atlassian.com/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "code": code,
                "redirect_uri": settings.jira_redirect_uri,
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        res_resp = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        res_resp.raise_for_status()
        resources_raw = res_resp.json()

        sites = [JiraSiteResource(**r) for r in resources_raw]
        session = get_or_create_session(session_id)
        session.connected = len(sites) > 0
        session.auth_mode = "oauth2"
        session.access_token = access_token
        session.available_sites = sites

        if sites:
            session.cloud_id = sites[0].id
            session.site_name = sites[0].name
            session.site_url = sites[0].url

        SESSION_STORE[session_id] = session
        return session


async def connect_via_pat(session_id: str, req: PATConnectRequest) -> JiraSessionState:
    """Fallback method allowing instant testing with Email + Atlassian API Token."""
    clean_url = req.site_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{clean_url}/rest/api/3/myself",
            auth=(req.email, req.api_token),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        user_info = resp.json()

    session = get_or_create_session(session_id)
    session.connected = True
    session.auth_mode = "pat"
    session.site_url = clean_url
    session.site_name = f"{clean_url.split('//')[-1]} ({user_info.get('displayName', req.email)})"
    session.email = req.email
    session.access_token = req.api_token
    SESSION_STORE[session_id] = session
    return session