from typing import Callable, List, Optional

from backend.auth_jira import get_or_create_session
from backend.trackers.base import IssueTracker
from backend.trackers.jira_tracker import JiraTracker


def _jira_tracker(session_id: str) -> Optional[IssueTracker]:
    session = get_or_create_session(session_id)
    return JiraTracker(session) if session.connected else None


# To add a provider (e.g. Azure Boards), implement IssueTracker and append its factory here.
TRACKER_PROVIDERS: List[Callable[[str], Optional[IssueTracker]]] = [
    _jira_tracker,
]


def get_active_tracker(session_id: str) -> Optional[IssueTracker]:
    for provider in TRACKER_PROVIDERS:
        tracker = provider(session_id)
        if tracker is not None:
            return tracker
    return None
