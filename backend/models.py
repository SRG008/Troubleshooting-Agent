from typing import List, Optional, Union
from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    role: str = Field(..., examples=["user", "assistant"])
    content: str


class ChatRequest(BaseModel):
    session_id: str = "default-engineer-session"
    message: str = Field(..., min_length=1, examples=["How many bugs are in the active sprint?"])
    history: List[ChatTurn] = Field(default_factory=list)


class AskAnswerRequest(BaseModel):
    session_id: str = "default-engineer-session"
    pending_id: str = Field(..., min_length=1)
    answer: Optional[Union[bool, float, str]] = None


class AgentStepTrace(BaseModel):
    step_name: str
    tool_used: str
    status: str
    details: str


class ChatResponse(BaseModel):
    answer: str
    provider: Optional[str] = None
    execution_trace: List[AgentStepTrace] = Field(default_factory=list)