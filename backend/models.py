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


class JiraTicketResponse(BaseModel):
    ticket_key: str = Field(..., alias="key")
    summary: str
    description: Optional[str] = None
    status: str
    priority: str
    issue_type: str = "Task"
    reporter: str = "System Board"
    created: str = ""
    labels: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    comments: List[str] = Field(default_factory=list)
    attachments: List[JiraAttachment] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class ChatTurn(BaseModel):
    role: str = Field(..., examples=["user", "assistant"])
    content: str


class ChatRequest(BaseModel):
    session_id: str = "default-engineer-session"
    message: str = Field(..., min_length=1, examples=["How many bugs are in the active sprint?"])
    history: List[ChatTurn] = Field(default_factory=list)


class AgentStepTrace(BaseModel):
    step_name: str
    tool_used: str
    status: str
    details: str


class ChatResponse(BaseModel):
    answer: str
    provider: Optional[str] = None
    execution_trace: List[AgentStepTrace] = Field(default_factory=list)