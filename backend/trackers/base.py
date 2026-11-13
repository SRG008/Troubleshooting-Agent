from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class IssueTracker(ABC):
    """Provider-neutral interface the AI agent uses to query an issue tracker.

    Every provider (Jira, Azure Boards, ...) implements these generic operations, so the
    agent never needs provider-specific logic. `query_language` and `query_guide` tell the
    model how to write native queries for this provider.
    """

    name: str
    query_language: str
    query_guide: str
    workspace_name: str

    @abstractmethod
    async def list_projects(self) -> List[Dict[str, Any]]:
        """All projects/spaces the user can access."""

    @abstractmethod
    async def list_boards(self, project_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Boards (scrum/kanban/team boards), optionally limited to one project."""

    @abstractmethod
    async def list_sprints(self, board_id: str, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Sprints/iterations of a board, optionally filtered by state (active, future, closed)."""

    @abstractmethod
    async def search_work_items(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Work items matching a query written in `query_language`."""

    @abstractmethod
    async def count_work_items(self, query: str) -> int:
        """Number of work items matching a query written in `query_language`."""

    @abstractmethod
    async def get_work_item(self, item_id: str) -> Dict[str, Any]:
        """Full details (description, comments, ...) of one work item."""
