from typing import TypedDict, List, Optional, Callable, Dict, Awaitable
from google import genai
from backend.config import settings
from backend.models import JiraTicketData, AgentStepTrace, JiraSessionState
from backend.jira_service import JiraClientService
from backend.rag_engine import hybrid_retriever


class IncidentAgentState(TypedDict):
    session: JiraSessionState
    ticket_key: str
    cloud_hint: str
    ticket_data: Optional[JiraTicketData]
    detected_cloud: str
    affected_resource_type: str
    cloud_telemetry_context: str
    rag_code_context: str
    execution_trace: List[AgentStepTrace]
    root_cause_analysis: str
    recommended_fix: str
    draft_jira_comment: str


class NativeAgentStateGraph:
    """
    Zero-C-Dependency Async StateGraph Orchestrator (100% compatible with Python 3.15+ on Windows).
    Provides the exact same node/edge state machine API without requiring C++ build tools for zstandard.
    """
    def __init__(self):
        self.nodes: Dict[str, Callable[[IncidentAgentState], Awaitable[IncidentAgentState]]] = {}
        self.edges: Dict[str, str] = {}
        self.entry_point: Optional[str] = None

    def add_node(self, name: str, func: Callable[[IncidentAgentState], Awaitable[IncidentAgentState]]):
        self.nodes[name] = func

    def set_entry_point(self, name: str):
        self.entry_point = name

    def add_edge(self, start_node: str, end_node: str):
        self.edges[start_node] = end_node

    async def ainvoke(self, state: IncidentAgentState) -> IncidentAgentState:
        current = self.entry_point
        while current and current != "END":
            node_fn = self.nodes[current]
            state = await node_fn(state)
            current = self.edges.get(current, "END")
        return state


async def node_fetch_jira_ticket(state: IncidentAgentState) -> IncidentAgentState:
    """Node 1: Connects to Jira API and extracts ticket description, comments, and attachments."""
    jira_service = JiraClientService(state["session"])
    ticket = await jira_service.fetch_ticket_details(state["ticket_key"])

    att_names = [a.filename for a in ticket.attachments]
    trace_msg = (
        f"Fetched '{ticket.summary}' [Status: {ticket.status}, Priority: {ticket.priority}]. "
        f"Comments: {len(ticket.comments)}, Attachments: {len(att_names)} ({', '.join(att_names) if att_names else 'None'})."
    )

    state["ticket_data"] = ticket
    state["execution_trace"].append(
        AgentStepTrace(
            step_name="1. Jira Intake & Attachment Parser",
            tool_used="Jira REST API v3",
            status="SUCCESS",
            details=trace_msg,
        )
    )
    return state


async def node_detect_cloud_and_resource(state: IncidentAgentState) -> IncidentAgentState:
    """Node 2: Inspects ticket payload to classify Cloud Provider (Azure vs AWS) & resource type."""
    ticket = state["ticket_data"]
    if not ticket:
        return state

    combined_text = f"{ticket.summary} {ticket.description} {' '.join(ticket.labels)} {' '.join(ticket.comments)}".lower()

    if state["cloud_hint"] in ("AZURE", "AWS"):
        cloud = state["cloud_hint"]
    elif any(k in combined_text for k in ["azure", "app service", "function app", "blob", "service bus", "cosmos"]):
        cloud = "AZURE"
    elif any(k in combined_text for k in ["aws", "lambda", "s3", "sqs", "cloudwatch", "ec2", "ecs"]):
        cloud = "AWS"
    else:
        cloud = "AZURE (Default Enterprise Profile)"

    if "function" in combined_text or "lambda" in combined_text:
        resource = "Serverless Function (Azure Function App / AWS Lambda)"
    elif "queue" in combined_text or "service bus" in combined_text or "sqs" in combined_text:
        resource = "Message Queue (Azure Service Bus / AWS SQS)"
    elif "blob" in combined_text or "s3" in combined_text or "bucket" in combined_text or "storage" in combined_text:
        resource = "Object Storage (Azure Blob Storage / AWS S3 Bucket)"
    elif "vm" in combined_text or "ec2" in combined_text or "virtual machine" in combined_text:
        resource = "Compute VM (Azure VM / AWS EC2)"
    else:
        resource = "Web Application / API Microservice (Azure App Service)"

    state["detected_cloud"] = cloud
    state["affected_resource_type"] = resource
    state["cloud_telemetry_context"] = (
        f"[Stage-2 Cloud Hook Ready] Target identified as {cloud} -> {resource}. "
        "Ready to bind Azure Monitor / AWS CloudWatch SDK credentials."
    )

    state["execution_trace"].append(
        AgentStepTrace(
            step_name="2. Multi-Cloud Topology & Resource Classifier",
            tool_used="Cloud Environment Discovery Router",
            status="SUCCESS",
            details=f"Provider: {cloud} | Target Architecture: {resource}",
        )
    )
    return state


async def node_hybrid_rag_search(state: IncidentAgentState) -> IncidentAgentState:
    """Node 3: Queries internal architecture runbooks & codebase index (BM25 + Vector RRF)."""
    ticket = state["ticket_data"]
    if not ticket:
        return state

    query = f"{ticket.summary} {ticket.description} {state['detected_cloud']} {state['affected_resource_type']}"
    retrieved_context = hybrid_retriever.search(query, top_k=2)
    state["rag_code_context"] = retrieved_context

    state["execution_trace"].append(
        AgentStepTrace(
            step_name="3. Hybrid RAG Knowledge & Codebase Lookup",
            tool_used="BM25 + Vector RRF Retriever",
            status="SUCCESS",
            details=f"Retrieved top runbook chunks matching {ticket.ticket_key}.",
        )
    )
    return state


async def node_gemini_synthesis(state: IncidentAgentState) -> IncidentAgentState:
    """Node 4: Synthesizes Jira ticket, cloud topology, and RAG context into Root Cause & Fix."""
    ticket = state["ticket_data"]
    if not ticket:
        return state

    client = genai.Client(api_key=settings.gemini_api_key)

    prompt = f"""
You are an expert Principal Site Reliability Engineer and Autonomous Incident Response Agent.
Analyze the following live Jira Production Ticket and provide a structured Root Cause Analysis and Resolution Plan.

### JIRA TICKET METADATA
- **Ticket ID:** {ticket.ticket_key}
- **Summary:** {ticket.summary}
- **Status / Priority:** {ticket.status} / {ticket.priority}
- **Description:** {ticket.description}
- **Comments:** {ticket.comments}
- **Attachments Present:** {[a.filename for a in ticket.attachments]}

### DETECTED INFRASTRUCTURE
- **Cloud Provider:** {state['detected_cloud']}
- **Resource Type:** {state['affected_resource_type']}
- **Architecture Context:** {state['rag_code_context']}

Respond in three clearly separated sections using these exact headings:
## ROOT CAUSE ANALYSIS
(Explain the technical root cause clearly based on the ticket evidence and cloud resource architecture)

## RECOMMENDED FIX
(Provide concrete step-by-step engineering remediation, configuration checks, and code patch patterns)

## DRAFT JIRA COMMENT
(Write a concise, ready-to-post engineering update for the Jira ticket)
"""

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
    )
    full_text = response.text or ""

    rca, fix, comment = full_text, "See Root Cause section above.", "Investigation completed by Agentic Ops."
    if "## ROOT CAUSE ANALYSIS" in full_text and "## RECOMMENDED FIX" in full_text:
        parts = full_text.split("## RECOMMENDED FIX")
        rca = parts[0].replace("## ROOT CAUSE ANALYSIS", "").strip()
        if "## DRAFT JIRA COMMENT" in parts[1]:
            subparts = parts[1].split("## DRAFT JIRA COMMENT")
            fix = subparts[0].strip()
            comment = subparts[1].strip()
        else:
            fix = parts[1].strip()

    state["root_cause_analysis"] = rca
    state["recommended_fix"] = fix
    state["draft_jira_comment"] = comment

    state["execution_trace"].append(
        AgentStepTrace(
            step_name="4. Root Cause & Remediation Synthesis",
            tool_used=f"Google Gemini ({settings.gemini_model})",
            status="SUCCESS",
            details="Generated Root Cause Analysis, Code Fix, and Draft Jira Comment.",
        )
    )
    return state


def build_incident_graph() -> NativeAgentStateGraph:
    workflow = NativeAgentStateGraph()
    workflow.add_node("fetch_jira", node_fetch_jira_ticket)
    workflow.add_node("detect_cloud", node_detect_cloud_and_resource)
    workflow.add_node("rag_search", node_hybrid_rag_search)
    workflow.add_node("synthesize", node_gemini_synthesis)

    workflow.set_entry_point("fetch_jira")
    workflow.add_edge("fetch_jira", "detect_cloud")
    workflow.add_edge("detect_cloud", "rag_search")
    workflow.add_edge("rag_search", "synthesize")
    workflow.add_edge("synthesize", "END")
    return workflow


incident_graph = build_incident_graph()