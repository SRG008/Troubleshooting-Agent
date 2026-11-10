from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from backend.config import settings
from backend.models import (
    JiraSessionState,
    PATConnectRequest,
    InvestigateRequest,
    InvestigationReport,
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

app = FastAPI(title="FDE Agentic Ops - Production Troubleshooter API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/auth/jira/status", response_model=JiraSessionState)
async def get_jira_status(session_id: str = Query(default="default-engineer-session")):
    """Checks if the user's UI session is currently authenticated with Jira."""
    return get_or_create_session(session_id)


@app.post("/auth/jira/disconnect", response_model=JiraSessionState)
async def jira_disconnect(session_id: str = Query(default="default-engineer-session")):
    """Disconnects the Jira session."""
    return disconnect_session(session_id)


@app.get("/auth/jira/login")
async def jira_oauth_login(session_id: str = Query(default="default-engineer-session")):
    """Redirects user to Atlassian's universal OAuth 2.0 login page."""
    if not settings.jira_client_id or not settings.jira_client_secret:
        raise HTTPException(
            status_code=400,
            detail="JIRA_CLIENT_ID and JIRA_CLIENT_SECRET must be set in .env for OAuth 2.0 flow.",
        )
    auth_url = build_atlassian_oauth_url(session_id)
    return RedirectResponse(url=auth_url)


@app.get("/auth/jira/callback")
async def jira_oauth_callback(code: str, state: str = "default-engineer-session"):
    """Exchanges code for token and redirects cleanly to the frontend UI with action flag."""
    await exchange_code_and_resolve_cloud_id(code=code, session_id=state)
    return RedirectResponse(url=f"{settings.frontend_url}?action=connected")


@app.post("/auth/jira/switch-site", response_model=JiraSessionState)
async def switch_jira_site(site_id: str = Query(...), session_id: str = Query(default="default-engineer-session")):
    """Switches the active Jira Cloud site for multi-site enterprise workspaces."""
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
    """Connects to Jira immediately using Site URL + Email + API Token."""
    return await connect_via_pat(session_id=session_id, req=req)


@app.get("/jira/tickets")
async def list_recent_tickets(session_id: str = Query(default="default-engineer-session")):
    """Fetches recent tickets across all permitted projects from the connected Jira workspace."""
    session = get_or_create_session(session_id)
    service = JiraClientService(session)
    return await service.fetch_recent_tickets(max_results=50)


@app.post("/agent/investigate", response_model=InvestigationReport)
async def run_incident_investigation(req: InvestigateRequest):
    """Triggers the multi-step agent state graph to inspect the Jira ticket and synthesize a fix."""
    session = get_or_create_session(req.session_id)
    if not session.connected:
        raise HTTPException(status_code=401, detail="Please connect to Jira first.")

    initial_state: IncidentAgentState = {
        "session": session,
        "ticket_key": req.ticket_key.strip().upper(),
        "cloud_hint": req.cloud_provider_hint,
        "ticket_data": None,
        "detected_cloud": "",
        "affected_resource_type": "",
        "cloud_telemetry_context": "",
        "rag_code_context": "",
        "execution_trace": [],
        "root_cause_analysis": "",
        "recommended_fix": "",
        "draft_jira_comment": "",
    }

    final_state = await incident_graph.ainvoke(initial_state)
    ticket_data = final_state["ticket_data"]
    if not ticket_data:
        raise HTTPException(status_code=404, detail="Failed to retrieve ticket data during agent execution.")

    return InvestigationReport(
        ticket_data=ticket_data,
        detected_cloud=final_state["detected_cloud"],
        affected_resource_type=final_state["affected_resource_type"],
        execution_trace=final_state["execution_trace"],
        root_cause_analysis=final_state["root_cause_analysis"],
        recommended_fix=final_state["recommended_fix"],
        draft_jira_comment=final_state["draft_jira_comment"],
    )