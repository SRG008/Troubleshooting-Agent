import httpx
from typing import Dict, Any, List, Optional, Tuple
from fastapi import HTTPException
from backend.models import JiraSessionState, JiraTicketData, JiraAttachment


def _extract_text_from_adf(adf_node: Any) -> str:
    """Recursively extracts clean text from Atlassian Document Format (ADF) JSON."""
    if not adf_node:
        return ""
    if isinstance(adf_node, str):
        return adf_node
    if isinstance(adf_node, dict):
        text_parts = []
        if adf_node.get("type") == "text" and "text" in adf_node:
            text_parts.append(adf_node["text"])
        for child in adf_node.get("content", []):
            text_parts.append(_extract_text_from_adf(child))
        return " ".join(filter(None, text_parts))
    if isinstance(adf_node, list):
        return "\n".join(_extract_text_from_adf(item) for item in adf_node)
    return ""


class JiraClientService:
    def __init__(self, session: JiraSessionState):
        if not session.connected:
            raise HTTPException(status_code=401, detail="Jira session is not connected.")
        self.session = session

    def _get_base_url_and_auth(
        self, api_root: str = "rest/api/3"
    ) -> Tuple[str, Dict[str, str], Optional[Tuple[str, str]]]:
        if self.session.auth_mode == "oauth2":
            base_url = f"https://api.atlassian.com/ex/jira/{self.session.cloud_id}/{api_root}"
            headers = {
                "Authorization": f"Bearer {self.session.access_token}",
                "Accept": "application/json",
            }
            auth = None
        else:
            base_url = f"{self.session.site_url}/{api_root}"
            headers = {"Accept": "application/json"}
            auth = (self.session.email or "", self.session.access_token or "")
        return base_url, headers, auth

    async def fetch_projects(self) -> List[Dict[str, str]]:
        """Fetches all permitted projects in the connected Jira workspace."""
        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=20.0) as client:
            request_kwargs: Dict[str, Any] = {"headers": headers}
            if auth is not None:
                request_kwargs["auth"] = auth

            resp = await client.get(f"{base_url}/project", **request_kwargs)
            if resp.status_code != 200:
                return []

            projects_data = resp.json()
            return [
                {"key": p.get("key"), "name": p.get("name")}
                for p in projects_data
                if p.get("key")
            ]

    async def fetch_recent_tickets(
        self, start_at: int = 0, max_results: int = 10, project_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Queries Atlassian's /rest/api/3/search/jql endpoint using cursor pagination
        and returns the exact [start_at : start_at + max_results] slice + total count.
        """
        base_url, headers, auth = self._get_base_url_and_auth()

        if project_key and project_key != "ALL":
            jql_query = f'project = "{project_key}" ORDER BY created DESC'
        else:
            jql_query = "project is not EMPTY ORDER BY created DESC"

        all_issues: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=25.0) as client:
            # Fetch up to 200 recent issues using Jira's cursor token so total count & pages are exact
            while len(all_issues) < 200:
                params: Dict[str, Any] = {
                    "jql": jql_query,
                    "maxResults": 100,
                    "fields": "summary,status,priority,issuetype,created,project",
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                request_kwargs: Dict[str, Any] = {
                    "params": params,
                    "headers": headers,
                }
                if auth is not None:
                    request_kwargs["auth"] = auth

                resp = await client.get(f"{base_url}/search/jql", **request_kwargs)
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=f"Jira API Search Error: {resp.text}",
                    )

                data = resp.json()
                batch = data.get("issues", [])
                all_issues.extend(batch)

                next_page_token = data.get("nextPageToken")
                is_last = data.get("isLast", True)
                if not next_page_token or is_last or not batch:
                    break

        total_count = len(all_issues)
        sliced_issues = all_issues[start_at : start_at + max_results]

        results = []
        for issue in sliced_issues:
            f = issue.get("fields", {})
            proj = f.get("project", {}) or {}
            results.append({
                "key": issue.get("key"),
                "project_key": proj.get("key", "UNKNOWN"),
                "project_name": proj.get("name", "Unknown Project"),
                "summary": f.get("summary", "No summary"),
                "status": (f.get("status") or {}).get("name", "Unknown"),
                "priority": (f.get("priority") or {}).get("name", "Medium"),
                "type": (f.get("issuetype") or {}).get("name", "Task"),
            })

        return {
            "issues": results,
            "total": total_count,
            "start_at": start_at,
            "max_results": max_results,
        }

    async def fetch_ticket_details(self, ticket_key: str) -> JiraTicketData:
        """Fetches full issue details, comments, and attachments for agentic analysis."""
        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=20.0) as client:
            request_kwargs: Dict[str, Any] = {"headers": headers}
            if auth is not None:
                request_kwargs["auth"] = auth

            resp = await client.get(f"{base_url}/issue/{ticket_key}", **request_kwargs)
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Ticket '{ticket_key}' not found in Jira.")
            resp.raise_for_status()
            data = resp.json()

        fields = data.get("fields", {})
        description_text = _extract_text_from_adf(fields.get("description"))

        raw_comments = fields.get("comment", {}).get("comments", [])
        comments = [
            f"{c.get('author', {}).get('displayName', 'User')}: {_extract_text_from_adf(c.get('body'))}"
            for c in raw_comments
        ]

        raw_attachments = fields.get("attachment", [])
        attachments = [
            JiraAttachment(
                id=str(att.get("id")),
                filename=att.get("filename", "unknown"),
                mime_type=att.get("mimeType", "application/octet-stream"),
                content_url=att.get("content", ""),
                size=att.get("size", 0),
            )
            for att in raw_attachments
        ]

        return JiraTicketData(
            ticket_key=data.get("key", ticket_key),
            summary=fields.get("summary", ""),
            description=description_text or "No description provided.",
            status=(fields.get("status") or {}).get("name", "Unknown"),
            priority=(fields.get("priority") or {}).get("name", "Unspecified"),
            issue_type=(fields.get("issuetype") or {}).get("name", "Bug"),
            reporter=(fields.get("reporter") or {}).get("displayName", "Unknown"),
            created=fields.get("created", ""),
            labels=fields.get("labels", []),
            components=[c.get("name", "") for c in fields.get("components", [])],
            comments=comments,
            attachments=attachments,
        )

    async def fetch_boards(self, project_key: Optional[str] = None, max_results: int = 200) -> List[Dict[str, Any]]:
        """Lists agile boards (Scrum/Kanban), optionally limited to one project."""
        base_url, headers, auth = self._get_base_url_and_auth("rest/agile/1.0")
        params: Dict[str, Any] = {}
        if project_key:
            params["projectKeyOrId"] = project_key
        boards = await self._paginate_offset(f"{base_url}/board", params, headers, auth, max_results)
        return [
            {
                "id": str(b.get("id")),
                "name": b.get("name"),
                "type": b.get("type"),
                "project_key": (b.get("location") or {}).get("projectKey"),
            }
            for b in boards
        ]

    async def fetch_sprints(
        self, board_id: str, state: Optional[str] = None, max_results: int = 200
    ) -> List[Dict[str, Any]]:
        """Lists sprints of a board, optionally filtered by state (active, future, closed)."""
        base_url, headers, auth = self._get_base_url_and_auth("rest/agile/1.0")
        params: Dict[str, Any] = {}
        if state:
            params["state"] = state
        sprints = await self._paginate_offset(
            f"{base_url}/board/{board_id}/sprint", params, headers, auth, max_results
        )
        return [
            {
                "id": str(s.get("id")),
                "name": s.get("name"),
                "state": s.get("state"),
                "start_date": s.get("startDate"),
                "end_date": s.get("endDate"),
                "goal": s.get("goal"),
            }
            for s in sprints
        ]

    async def search_issues(self, jql: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Runs a JQL query and returns up to max_results issues in a compact form."""
        base_url, headers, auth = self._get_base_url_and_auth()
        limit = max(1, min(max_results, 100))
        issues: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=25.0) as client:
            while len(issues) < limit:
                params: Dict[str, Any] = {
                    "jql": jql,
                    "maxResults": min(100, limit - len(issues)),
                    "fields": _ISSUE_FIELDS,
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                resp = await client.get(f"{base_url}/search/jql", params=params, headers=headers, auth=auth)
                _ensure_ok(resp)
                data = resp.json()
                batch = data.get("issues", [])
                issues.extend(batch)

                next_page_token = data.get("nextPageToken")
                if not next_page_token or data.get("isLast", True) or not batch:
                    break

        return [_map_issue(i) for i in issues[:limit]]

    async def count_issues(self, jql: str) -> int:
        """Returns the number of issues matching a JQL query."""
        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{base_url}/search/approximate-count", json={"jql": jql}, headers=headers, auth=auth
            )
        _ensure_ok(resp)
        return int(resp.json().get("count", 0))

    async def _paginate_offset(
        self,
        url: str,
        params: Dict[str, Any],
        headers: Dict[str, str],
        auth: Optional[Tuple[str, str]],
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Collects values from an offset-paginated Agile endpoint (startAt / isLast)."""
        items: List[Dict[str, Any]] = []
        start_at = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            while len(items) < max_results:
                page_params = {**params, "startAt": start_at, "maxResults": min(50, max_results - len(items))}
                resp = await client.get(url, params=page_params, headers=headers, auth=auth)
                _ensure_ok(resp)
                data = resp.json()
                values = data.get("values", [])
                items.extend(values)
                start_at += len(values)
                if data.get("isLast", True) or not values:
                    break
        return items[:max_results]


_ISSUE_FIELDS = "summary,status,priority,issuetype,project,assignee,created,updated,duedate,labels"


def _ensure_ok(resp: httpx.Response) -> None:
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Jira API error: {resp.text}")


def _map_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    f = issue.get("fields", {}) or {}
    proj = f.get("project") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "type": (f.get("issuetype") or {}).get("name"),
        "status": (f.get("status") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "project_key": proj.get("key"),
        "project_name": proj.get("name"),
        "labels": f.get("labels", []),
        "due_date": f.get("duedate"),
        "created": f.get("created"),
        "updated": f.get("updated"),
    }