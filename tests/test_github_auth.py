import asyncio
import unittest
import urllib.parse
from unittest.mock import patch

import httpx

from backend import auth_github
from backend.auth_github import (
    GitHubConnectionError,
    build_github_oauth_url,
    complete_github_connection,
    get_github_status,
)
from backend.config import settings

ORG = "acme"


def _repo(owner, name, private=True):
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": private,
        "html_url": f"https://github.com/{owner}/{name}",
        "owner": {"login": owner},
    }


def _github(membership_status=200, repos=()):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "token-123"})
        if path == "/user":
            return httpx.Response(200, json={"login": "alice"})
        if path == f"/user/memberships/orgs/{ORG}":
            if membership_status != 200:
                return httpx.Response(membership_status)
            return httpx.Response(200, json={"state": "active"})
        if path == "/user/repos":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=list(repos) if page == 1 else [])
        return httpx.Response(404)

    return handle


class GitHubConnectionTests(unittest.TestCase):
    def setUp(self):
        auth_github.GITHUB_SESSION_STORE.clear()
        auth_github.PENDING_OAUTH_STATES.clear()
        for name, value in (
            ("github_client_id", "client-id"),
            ("github_client_secret", "client-secret"),
            ("github_organization", ORG),
        ):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _state_for(self, session_id):
        url = build_github_oauth_url(session_id)
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    def _complete(self, handler, state):
        def client_factory():
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=auth_github.GITHUB_API_HEADERS)

        with patch("backend.auth_github._http_client", side_effect=client_factory):
            return asyncio.run(complete_github_connection(code="code", state=state))

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(GitHubConnectionError):
            self._complete(_github(), state="forged-state")

    def test_non_member_is_refused_and_not_connected(self):
        state = self._state_for("session-1")
        with self.assertRaises(GitHubConnectionError):
            self._complete(_github(membership_status=404), state)
        self.assertFalse(get_github_status("session-1").connected)

    def test_only_organization_repositories_are_listed_and_token_is_hidden(self):
        repos = [_repo("acme", "api"), _repo("alice", "personal"), _repo("acme", "web", private=False)]
        state = self._state_for("session-1")
        self._complete(_github(repos=repos), state)

        status = get_github_status("session-1")
        self.assertTrue(status.connected)
        self.assertEqual(status.login, "alice")
        self.assertEqual([r.full_name for r in status.repositories], ["acme/api", "acme/web"])
        self.assertNotIn("token-123", status.model_dump_json())

    def test_connection_is_isolated_per_session(self):
        state = self._state_for("session-1")
        self._complete(_github(repos=[_repo("acme", "api")]), state)
        self.assertFalse(get_github_status("session-2").connected)


if __name__ == "__main__":
    unittest.main()
