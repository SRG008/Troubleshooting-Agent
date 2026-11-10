from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class JiraSiteResource(BaseModel):
    id: str = Field(..., description="Atlassian Cloud ID")
    name: str = Field(..., description="Workspace/Site Name")
    url: str = Field(..., description="Jira Site URL")
    scopes: List[str] = Field(default_factory=list)


class JiraSessionState(BaseModel):
    connected: bool = False
    auth_mode: str = "none"  # "oauth2" or "pat"
    access_token: Optional[str] = None
    cloud_id: Optional[str] = None
    site_url: Optional[str] = None
    site_name: Optional[str] = None
    email: Optional[str] = None
    available_sites: List[JiraSiteResource] = Field(default_factory=list)


class PATConnectRequest(BaseModel):
    site_url: str = Field(..., examples=["https://your-domain.atlassian.net"])
    email: str = Field(..., examples=["engineer@company.com"])
    api_token: str = Field(..., description="Atlassian API Token")


class JiraAttachment(BaseModel):
    id: str
    filename: str
    mime_type: str
    content_url: str
    size: int


class JiraTicketData(BaseModel):
    ticket_key: str
    summary: str
    description: str
    status: str
    priority: str
    issue_type: str
    reporter: str
    created: str
    labels: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    comments: List[str] = Field(default_factory=list)
    attachments: List[JiraAttachment] = Field(default_factory=list)


class InvestigateRequest(BaseModel):
    session_id: str = "default-engineer-session"
    ticket_key: str = Field(..., examples=["PROD-101"])
    cloud_provider_hint: str = Field(default="AUTO", description="AUTO, AZURE, or AWS")


class AgentStepTrace(BaseModel):
    step_name: str
    tool_used: str
    status: str
    details: str


class InvestigationReport(BaseModel):
    ticket_data: JiraTicketData
    detected_cloud: str
    affected_resource_type: str
    execution_trace: List[AgentStepTrace]
    root_cause_analysis: str
    recommended_fix: str
    draft_jira_comment: str
