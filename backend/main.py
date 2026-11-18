from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import json
import logging
import urllib.parse
from typing import Optional
from backend.config import settings
from backend.network import configure_outbound_network

configure_outbound_network()

from backend.models import (
    ChatRequest,
    ChatResponse,
    AskAnswerRequest,
)
from backend.atlassian_auth import (
    APITokenConnectRequest,
    AtlassianAuthError,
    AtlassianStatus,
    build_oauth_url,
    complete_oauth,
    connect_api_token,
    disconnect_atlassian,
    discard_oauth_state,
    drop_connection,
    get_atlassian_status,
    select_site,
)
from backend.atlassian_mcp import AtlassianMCPError, AtlassianRejectedError, forget_tools, load_workspace
from backend.auth_github import (
    GitHubConnectionError,
    GitHubSelectionRequest,
    GitHubStatus,
    build_github_oauth_url,
    complete_github_connection,
    disconnect_github,
    discard_oauth_state,
    get_github_status,
    set_selected_repositories,
)
from backend.github_workspace import pick_folder
from backend.session_cookie import redirect_with_session
from backend.agent import answer_question, stream_answer, resume_answer
from backend.cloud_connector import (
    CloudSessionState,
    AzureConnectRequest,
    AWSConnectRequest,
    CloudTelemetryConnector,
    get_or_create_cloud_session,
    disconnect_cloud_session,
)

app = FastAPI(title="FDE Agentic Ops - Production Troubleshooter API", version="2.1.0")
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- STAGE 1: JIRA ENDPOINTS (Atlassian Rovo MCP server) ----------------
def _jira_failed(reason: str) -> RedirectResponse:
    query = urllib.parse.urlencode({"action": "jira_failed", "reason": reason})
    return RedirectResponse(url=f"{settings.frontend_url}?{query}")


@app.get("/auth/jira/status", response_model=AtlassianStatus)
async def get_jira_status(session_id: str = Query(default="default-engineer-session")):
    status = get_atlassian_status(session_id)
    if status.connected and not status.sites:
        # Connections saved before their site list could be read get it now.
        try:
            await load_workspace(session_id)
        except (AtlassianAuthError, AtlassianMCPError) as e:
            logger.warning("Could not load the Atlassian sites: %s", e)
        status = get_atlassian_status(session_id)
    return status


@app.post("/auth/jira/disconnect", response_model=AtlassianStatus)
async def jira_disconnect(session_id: str = Query(default="default-engineer-session")):
    forget_tools(session_id)
    return await disconnect_atlassian(session_id)


@app.get("/auth/jira/login")
async def jira_oauth_login(session_id: str = Query(default="default-engineer-session")):
    try:
        auth_url = await build_oauth_url(session_id)
    except AtlassianAuthError as e:
        return _jira_failed(str(e))
    except httpx.HTTPError as e:
        logger.error("Could not reach Atlassian to start the Jira sign-in: %r", e)
        return _jira_failed(f"Could not reach Atlassian ({type(e).__name__}). Check the network or HTTPS_PROXY in .env.")
    return RedirectResponse(url=auth_url)


@app.get("/auth/jira/callback")
async def jira_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error or not code or not state:
        if state:
            discard_oauth_state(state)
        return _jira_failed(error_description or error or "Atlassian did not return an authorization code.")
    session_id = None
    try:
        session_id = await complete_oauth(code=code, state=state)
        await load_workspace(session_id)
    except (AtlassianAuthError, AtlassianMCPError) as e:
        if session_id:
            drop_connection(session_id)
        return _jira_failed(str(e))
    except httpx.HTTPError as e:
        logger.error("Could not reach Atlassian to finish the Jira sign-in: %r", e)
        if session_id:
            drop_connection(session_id)
        return _jira_failed(f"Could not reach Atlassian ({type(e).__name__}). Check the network or HTTPS_PROXY in .env.")
    return redirect_with_session(f"{settings.frontend_url}?action=connected", session_id)


@app.post("/auth/jira/switch-site", response_model=AtlassianStatus)
async def switch_jira_site(site_id: str = Query(...), session_id: str = Query(default="default-engineer-session")):
    try:
        return select_site(session_id, site_id)
    except AtlassianAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/jira/connect-token", response_model=AtlassianStatus)
async def jira_connect_token(
    req: APITokenConnectRequest, session_id: str = Query(default="default-engineer-session")
):
    try:
        await connect_api_token(session_id, req)
        await load_workspace(session_id)
    except AtlassianRejectedError:
        drop_connection(session_id)
        raise HTTPException(
            status_code=400,
            detail=(
                "Atlassian rejected this email and API token (HTTP 401). Check that: "
                "1) API-token sign-in is turned on in Atlassian Administration > Connectors > Atlassian MCP server > "
                "Authentication; 2) the token was created with scopes for the Rovo MCP app, including "
                "read:jira:agent-interface, search:jira:agent-interface, read:me and read:account "
                "(a classic token without scopes does not work); "
                "3) the email is the one of the Atlassian account that created the token; "
                "4) the token was copied in full and has not expired."
            ),
        )
    except (AtlassianAuthError, AtlassianMCPError) as e:
        drop_connection(session_id)
        raise HTTPException(status_code=400, detail=str(e))
    return get_atlassian_status(session_id)


# ---------------- GITHUB ENDPOINTS ----------------
@app.get("/auth/github/status", response_model=GitHubStatus)
async def get_github_connection_status(session_id: str = Query(default="default-engineer-session")):
    return get_github_status(session_id)


@app.post("/auth/github/disconnect", response_model=GitHubStatus)
async def github_disconnect(session_id: str = Query(default="default-engineer-session")):
    return disconnect_github(session_id)


@app.put("/auth/github/selection", response_model=GitHubStatus)
async def github_select_repositories(
    req: GitHubSelectionRequest, session_id: str = Query(default="default-engineer-session")
):
    try:
        return set_selected_repositories(session_id, req.repositories)
    except GitHubConnectionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/github/login")
async def github_oauth_login(session_id: str = Query(default="default-engineer-session")):
    try:
        auth_url = build_github_oauth_url(session_id)
    except GitHubConnectionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=auth_url)


@app.get("/folders/browse")
async def browse_local_folder():
    try:
        folder = await asyncio.to_thread(pick_folder)
    except GitHubConnectionError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"folder": folder}


@app.get("/auth/github/callback")
async def github_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error or not code or not state:
        if state:
            discard_oauth_state(state)
        reason = error_description or error or "GitHub did not return an authorization code."
    else:
        try:
            session_id = await complete_github_connection(code=code, state=state)
            return redirect_with_session(f"{settings.frontend_url}?action=github_connected", session_id)
        except GitHubConnectionError as e:
            reason = str(e)
        except httpx.HTTPError:
            reason = "Could not reach GitHub. Try again."
    query = urllib.parse.urlencode({"action": "github_failed", "reason": reason})
    return RedirectResponse(url=f"{settings.frontend_url}?{query}")


# ---------------- STAGE 2: STANDALONE CLOUD CONNECTOR ENDPOINTS ----------------
@app.get("/cloud/status", response_model=CloudSessionState)
async def get_cloud_status(session_id: str = Query(default="default-engineer-session")):
    return get_or_create_cloud_session(session_id)


@app.post("/cloud/connect/azure", response_model=CloudSessionState)
async def connect_azure_account(
    req: AzureConnectRequest, session_id: str = Query(default="default-engineer-session")
):
    return await CloudTelemetryConnector.connect_azure(session_id=session_id, req=req)


@app.post("/cloud/connect/aws", response_model=CloudSessionState)
async def connect_aws_account(
    req: AWSConnectRequest, session_id: str = Query(default="default-engineer-session")
):
    return await CloudTelemetryConnector.connect_aws(session_id=session_id, req=req)


@app.post("/cloud/disconnect", response_model=CloudSessionState)
async def disconnect_cloud(session_id: str = Query(default="default-engineer-session")):
    return disconnect_cloud_session(session_id)


# ---------------- AGENT CHAT ENDPOINT ----------------
@app.post("/agent/chat", response_model=ChatResponse)
async def agent_chat(req: ChatRequest):
    return await answer_question(session_id=req.session_id, message=req.message, history=req.history)


@app.post("/agent/chat/stream")
async def agent_chat_stream(req: ChatRequest):
    """Server-sent events: one JSON object per 'data:' line (delta, reset, status, ask, done)."""
    return _sse(stream_answer(session_id=req.session_id, message=req.message, history=req.history))


@app.post("/agent/chat/resume")
async def agent_chat_resume(req: AskAnswerRequest):
    """Continues a run that paused on an 'ask' event. Same event stream as /agent/chat/stream."""
    return _sse(resume_answer(session_id=req.session_id, pending_id=req.pending_id, answer=req.answer))


def _sse(events_source):
    async def events():
        async for event in events_source:
            if "execution_trace" in event:
                event = {**event, "execution_trace": [step.model_dump() for step in event["execution_trace"]]}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
