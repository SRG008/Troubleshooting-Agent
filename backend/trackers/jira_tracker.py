from typing import Any, Dict, List, Optional

from backend.jira_service import JiraClientService
from backend.models import JiraSessionState
from backend.trackers.base import IssueTracker


class JiraTracker(IssueTracker):
    name = "Jira"
    query_language = "JQL (Jira Query Language)"
    query_guide = (
        'Examples: project = KAN AND status = "In Progress"; issuetype = Bug AND priority = High; '
        "assignee = currentUser() AND resolution = EMPTY; sprint = 12 (get IDs from list_sprints) "
        "or sprint in openSprints(); created >= -7d ORDER BY created DESC. "
        "Quote values that contain spaces."
    )

    def __init__(self, session: JiraSessionState):
        self._client = JiraClientService(session)
        self.workspace_name = session.site_name or "Jira workspace"

    async def list_projects(self) -> List[Dict[str, Any]]:
        return await self._client.fetch_projects()

    async def list_boards(self, project_key: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._client.fetch_boards(project_key=project_key)

    async def list_sprints(self, board_id: str, state: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._client.fetch_sprints(board_id=board_id, state=state)

    async def search_work_items(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        return await self._client.search_issues(jql=query, max_results=max_results)

    async def count_work_items(self, query: str) -> int:
        return await self._client.count_issues(jql=query)

    async def get_work_item(self, item_id: str) -> Dict[str, Any]:
        details = await self._client.fetch_ticket_details(item_id)
        return details.model_dump(exclude={"attachments"})
