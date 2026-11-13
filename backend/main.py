from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import json
from typing import Optional
from backend.config import settings
from backend.models import (
    JiraSessionState,
    PATConnectRequest,
    ChatRequest,
    ChatResponse,
    AskAnswerRequest,
)
from backend.auth_jira import (
    get_or_create_session,
    disconnect_session,
    build_atlassian_oauth_url,
    exchange_code_and_resolve_cloud_id,
    connect_via_pat,
)
from backend.jira_service import JiraClientService
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- STAGE 1: JIRA ENDPOINTS ----------------
@app.get("/auth/jira/status", response_model=JiraSessionState)
async def get_jira_status(session_id: str = Query(default="default-engineer-session")):
    return get_or_create_session(session_id)


@app.post("/auth/jira/disconnect", response_model=JiraSessionState)
async def jira_disconnect(session_id: str = Query(default="default-engineer-session")):
    return disconnect_session(session_id)


@app.get("/auth/jira/login")
async def jira_oauth_login(session_id: str = Query(default="default-engineer-session")):
    if not settings.jira_client_id or not settings.jira_client_secret:
        raise HTTPException(
            status_code=400,
            detail="JIRA_CLIENT_ID and JIRA_CLIENT_SECRET must be set in .env for OAuth 2.0 flow.",
        )
    auth_url = build_atlassian_oauth_url(session_id)
    return RedirectResponse(url=auth_url)


@app.get("/auth/jira/callback")
async def jira_oauth_callback(code: str, state: str = "default-engineer-session"):
    await exchange_code_and_resolve_cloud_id(code=code, session_id=state)
    return RedirectResponse(url=f"{settings.frontend_url}?action=connected")


@app.post("/auth/jira/switch-site", response_model=JiraSessionState)
async def switch_jira_site(site_id: str = Query(...), session_id: str = Query(default="default-engineer-session")):
    session = get_or_create_session(session_id)
    if not session.available_sites:
        raise HTTPException(status_code=400, detail="No available sites found in session.")

    selected_site = next((s for s in session.available_sites if s.id == site_id), None)
    if not selected_site:
        raise HTTPException(status_code=404, detail="Selected site not found.")

    session.cloud_id = selected_site.id
    session.site_name = selected_site.name
    session.site_url = selected_site.url
    return session


@app.post("/auth/jira/connect-pat", response_model=JiraSessionState)
async def jira_connect_pat(
    req: PATConnectRequest, session_id: str = Query(default="default-engineer-session")
):
    return await connect_via_pat(session_id=session_id, req=req)


@app.get("/jira/projects")
async def list_jira_projects(session_id: str = Query(default="default-engineer-session")):
    session = get_or_create_session(session_id)
    service = JiraClientService(session)
    return await service.fetch_projects()


@app.get("/jira/tickets")
async def list_recent_tickets(
    start_at: int = Query(default=0, ge=0),
    max_results: int = Query(default=10, ge=1, le=50),
    project_key: Optional[str] = Query(default=None),
    session_id: str = Query(default="default-engineer-session"),
):
    session = get_or_create_session(session_id)
    service = JiraClientService(session)
    return await service.fetch_recent_tickets(
        start_at=start_at, max_results=max_results, project_key=project_key
    )


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
