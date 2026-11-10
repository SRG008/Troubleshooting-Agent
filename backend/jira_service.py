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

    def _get_base_url_and_auth(self) -> Tuple[str, Dict[str, str], Optional[Tuple[str, str]]]:
        if self.session.auth_mode == "oauth2":
            base_url = f"https://api.atlassian.com/ex/jira/{self.session.cloud_id}/rest/api/3"
            headers = {
                "Authorization": f"Bearer {self.session.access_token}",
                "Accept": "application/json",
            }
            auth = None
        else:
            base_url = f"{self.session.site_url}/rest/api/3"
            headers = {"Accept": "application/json"}
            auth = (self.session.email or "", self.session.access_token or "")
        return base_url, headers, auth

    async def fetch_recent_tickets(self, max_results: int = 50) -> List[Dict[str, str]]:
        """Queries Atlassian's /rest/api/3/search/jql endpoint with a valid bounded JQL query."""
        base_url, headers, auth = self._get_base_url_and_auth()
        async with httpx.AsyncClient(timeout=25.0) as client:
            request_kwargs: Dict[str, Any] = {
                "json": {
                    # Valid bounded JQL query that matches all workspace tickets
                    "jql": "project is not EMPTY ORDER BY created DESC",
                    "maxResults": max_results,
                    "fields": ["summary", "status", "priority", "issuetype", "created"],
                },
                "headers": headers,
            }
            if auth is not None:
                request_kwargs["auth"] = auth

            resp = await client.post(f"{base_url}/search/jql", **request_kwargs)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Jira API Search Error: {resp.text}"
                )

            data = resp.json()
            issues = data.get("issues", [])
            results = []
            for issue in issues:
                f = issue.get("fields", {})
                results.append({
                    "key": issue.get("key"),
                    "summary": f.get("summary", "No summary"),
                    "status": (f.get("status") or {}).get("name", "Unknown"),
                    "priority": (f.get("priority") or {}).get("name", "Medium"),
                    "type": (f.get("issuetype") or {}).get("name", "Task"),
                })
            return results

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