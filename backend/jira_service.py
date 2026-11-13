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

    async def fetch_issue_types(self, project_key: str) -> List[Dict[str, Any]]:
        """Issue types that can be created in a project."""
        base_url, headers, auth = self._get_base_url_and_auth()
        types = await self._paginate_offset(
            f"{base_url}/issue/createmeta/{project_key}/issuetypes", {}, headers, auth, 200,
            collection_key="issueTypes",
        )
        return [{"id": str(t.get("id")), "name": t.get("name")} for t in types]

    async def fetch_required_fields(self, project_key: str, issue_type: str) -> List[Dict[str, Any]]:
        """Required creation fields of an issue type, with their allowed values where Jira defines them."""
        _, metas = await self._create_field_metadata(project_key, issue_type)
        return [
            _describe_field(meta)
            for meta in metas
            if meta.get("required") and meta.get("fieldId") not in _SYSTEM_FIELDS
        ]

    async def create_issue(self, project_key: str, issue_type: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Creates an issue. `values` maps field ids to plain answers, converted to Jira's format here."""
        issue_type_id, metas = await self._create_field_metadata(project_key, issue_type)
        by_id = {meta["fieldId"]: meta for meta in metas}

        unknown = [field_id for field_id in values if field_id not in by_id]
        if unknown:
            raise ValueError(f"Unknown field ids for {issue_type}: {', '.join(unknown)}.")
        missing = [
            meta.get("name", meta["fieldId"])
            for meta in metas
            if meta.get("required")
            and meta["fieldId"] not in _SYSTEM_FIELDS
            and not _has_value(values.get(meta["fieldId"]))
        ]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}.")

        fields: Dict[str, Any] = {"project": {"key": project_key}, "issuetype": {"id": issue_type_id}}
        for field_id, value in values.items():
            if field_id in _SYSTEM_FIELDS or not _has_value(value):
                continue
            fields[field_id] = _to_field_value(by_id[field_id], value)

        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{base_url}/issue", json={"fields": fields}, headers=headers, auth=auth)
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=resp.status_code, detail=f"Jira API error: {resp.text}")

        key = resp.json().get("key")
        url = f"{self.session.site_url}/browse/{key}" if self.session.site_url and key else None
        return {"key": key, "url": url}

    async def search_users(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """Active Atlassian users matching a name or email."""
        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{base_url}/user/search", params={"query": query, "maxResults": max_results}, headers=headers, auth=auth
            )
        _ensure_ok(resp)
        return [
            {"account_id": u.get("accountId"), "display_name": u.get("displayName")}
            for u in resp.json()
            if u.get("accountId") and u.get("active", True) and u.get("accountType", "atlassian") == "atlassian"
        ]

    async def _create_field_metadata(self, project_key: str, issue_type: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Returns the id of the named issue type and the raw creation metadata of all its fields."""
        base_url, headers, auth = self._get_base_url_and_auth()
        types = await self._paginate_offset(
            f"{base_url}/issue/createmeta/{project_key}/issuetypes", {}, headers, auth, 200,
            collection_key="issueTypes",
        )
        match = next((t for t in types if (t.get("name") or "").lower() == issue_type.lower()), None)
        if match is None:
            available = ", ".join(t.get("name", "?") for t in types) or "none"
            raise ValueError(f"Issue type '{issue_type}' not found in project {project_key}. Available: {available}.")
        issue_type_id = str(match["id"])
        metas = await self._paginate_offset(
            f"{base_url}/issue/createmeta/{project_key}/issuetypes/{issue_type_id}", {}, headers, auth, 500,
            collection_key="fields",
        )
        return issue_type_id, metas

    async def _paginate_offset(
        self,
        url: str,
        params: Dict[str, Any],
        headers: Dict[str, str],
        auth: Optional[Tuple[str, str]],
        max_results: int,
        collection_key: str = "values",
    ) -> List[Dict[str, Any]]:
        """Collects offset-paginated Agile or creation metadata results."""
        items: List[Dict[str, Any]] = []
        start_at = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            while len(items) < max_results:
                page_params = {**params, "startAt": start_at, "maxResults": min(50, max_results - len(items))}
                resp = await client.get(url, params=page_params, headers=headers, auth=auth)
                _ensure_ok(resp)
                data = resp.json()
                values = data.get(collection_key)
                if not isinstance(values, list):
                    raise ValueError(f"Jira API response is missing the '{collection_key}' result list.")
                items.extend(values)
                start_at += len(values)
                is_last = data.get("isLast")
                if is_last is None:
                    is_last = start_at >= data.get("total", start_at)
                if is_last or not values:
                    break
        return items[:max_results]


_ISSUE_FIELDS = "summary,status,priority,issuetype,project,assignee,created,updated,duedate,labels"


_SYSTEM_FIELDS = {"project", "issuetype"}


def _has_value(value: Any) -> bool:
    return value not in (None, "", [])


def _text_to_adf(text: str) -> Dict[str, Any]:
    """Wraps plain text in Atlassian Document Format, one paragraph per line."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line.strip()
    ]
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph", "content": []}]}


def _allowed_label(allowed: Dict[str, Any]) -> str:
    return str(allowed.get("name") or allowed.get("value") or allowed.get("id"))


def _describe_field(meta: Dict[str, Any]) -> Dict[str, Any]:
    schema = meta.get("schema") or {}
    kind = schema.get("type")
    if kind == "array":
        kind = f"array of {schema.get('items')}"
    described: Dict[str, Any] = {"field_id": meta["fieldId"], "name": meta.get("name"), "type": kind}
    allowed = [_allowed_label(a) for a in meta.get("allowedValues") or []]
    if allowed:
        described["allowed_values"] = allowed[:100]
    return described


def _option_ref(meta: Dict[str, Any], value: Any) -> Dict[str, str]:
    """Resolves a plain answer to a Jira reference (id) using the field's allowed values."""
    wanted = str(value).strip().lower()
    for allowed in meta.get("allowedValues") or []:
        labels = {str(allowed.get(k)).lower() for k in ("id", "name", "value") if allowed.get(k) is not None}
        if wanted in labels:
            return {"id": str(allowed["id"])}
    raise ValueError(f"'{value}' is not an allowed value for {meta.get('name', meta['fieldId'])}.")


def _to_field_value(meta: Dict[str, Any], value: Any) -> Any:
    """Converts a plain answer into the JSON shape Jira expects for this field's schema."""
    schema = meta.get("schema") or {}
    kind = schema.get("type")
    if meta["fieldId"] == "description" or kind == "doc":
        return _text_to_adf(str(value))
    if kind == "array":
        parts = value if isinstance(value, list) else str(value).split(",")
        parts = [str(p).strip() for p in parts if str(p).strip()]
        if schema.get("items") == "string":
            return parts
        return [_option_ref(meta, p) for p in parts]
    if kind in ("option", "priority", "version", "component", "resolution"):
        return _option_ref(meta, value)
    if kind == "user":
        return {"accountId": str(value)}
    if kind == "number":
        return float(value)
    return str(value)


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