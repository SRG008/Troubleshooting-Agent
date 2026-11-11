from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, cast
from backend.config import settings
from backend.models import (
    JiraSessionState,
    PATConnectRequest,
    InvestigateRequest,
    InvestigationReport,
    JiraTicketData,
)
from backend.auth_jira import (
    get_or_create_session,
    disconnect_session,
    build_atlassian_oauth_url,
    exchange_code_and_resolve_cloud_id,
    connect_via_pat,
)
from backend.jira_service import JiraClientService
from backend.agent_graph import incident_graph, IncidentAgentState
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


# ---------------- AGENT ORCHESTRATOR ENDPOINT ----------------
@app.post("/agent/investigate", response_model=InvestigationReport)
async def run_incident_investigation(req: InvestigateRequest):
    session = get_or_create_session(req.session_id)
    if not session.connected:
        raise HTTPException(status_code=401, detail="Please connect to Jira first.")

    cloud_session = get_or_create_cloud_session(req.session_id)

    initial_state = cast(IncidentAgentState, {
        "session": session,
        "ticket_key": req.ticket_key.strip().upper(),
        "user_query": req.ticket_key,
        "cloud_hint": cloud_session.provider or req.cloud_provider_hint,
        "ticket_data": None,
        "detected_cloud": "",
        "affected_resource_type": "",
        "execution_trace": [],
        "root_cause_analysis": "",
        "recommended_fix": "",
        "draft_jira_comment": "",
        "chat_response": "",
        "is_list_query": False,
        "is_investigation_query": False,
    })

    final_state = await incident_graph.ainvoke(initial_state)
    
    ticket_data = final_state["ticket_data"]
    if final_state.get("is_list_query"):
        ticket_data = JiraTicketData(
            ticket_key="BOARD",
            summary="Workspace Ticket Listing",
            description=final_state["chat_response"],
            status="Active",
            priority="Medium",
            issue_type="Task",
            reporter="AI Agent",
            created=""
        )
    elif not ticket_data:
        ticket_data = JiraTicketData(
            ticket_key=final_state.get("ticket_key", "UNKNOWN"),
            summary="Ticket Lookup Result",
            description=final_state["chat_response"],
            status="Unknown",
            priority="Medium",
            issue_type="Task",
            reporter="AI Agent",
            created=""
        )

    rca_text = final_state["root_cause_analysis"] if final_state.get("is_investigation_query") else final_state["chat_response"]
    fix_text = final_state["recommended_fix"] if final_state.get("is_investigation_query") else "No code patch generated (Detail lookup mode)."
    comment_text = final_state["draft_jira_comment"] if final_state.get("is_investigation_query") else f"Reviewed details for {ticket_data.ticket_key}."

    return InvestigationReport(
        ticket_data=ticket_data,
        detected_cloud=final_state["detected_cloud"] or "None",
        affected_resource_type=final_state["affected_resource_type"] or "N/A",
        execution_trace=final_state["execution_trace"],
        root_cause_analysis=rca_text,
        recommended_fix=fix_text,
        draft_jira_comment=comment_text,
    )