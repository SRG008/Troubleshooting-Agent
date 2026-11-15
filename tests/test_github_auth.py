import asyncio
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for target, value in (
            ("GITHUB_STATE_DIR", base),
            ("GITHUB_KEY_FILE", base / "store.key"),
            ("GITHUB_STORE_FILE", base / "connections.enc"),
        ):
            patcher = patch.object(auth_github, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
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

    def test_selection_is_limited_to_accessible_repositories(self):
        state = self._state_for("session-1")
        self._complete(_github(repos=[_repo("acme", "api"), _repo("acme", "web")]), state)

        status = auth_github.set_selected_repositories("session-1", ["acme/api", "acme/api"])
        self.assertEqual(status.selected_repositories, ["acme/api"])

        with self.assertRaises(GitHubConnectionError):
            auth_github.set_selected_repositories("session-1", ["acme/api", "acme/secret"])
        self.assertEqual(get_github_status("session-1").selected_repositories, ["acme/api"])


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        auth_github.GITHUB_SESSION_STORE.clear()
        auth_github.GITHUB_SESSION_STORE["session-1"] = auth_github._GitHubConnection(
            access_token="token-123", login="alice", user_id=7, selected=["acme/api"]
        )

    def _open(self, handler, repository="acme/api"):
        def client_factory():
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=auth_github.GITHUB_API_HEADERS)

        with patch("backend.auth_github._http_client", side_effect=client_factory):
            return asyncio.run(
                auth_github.open_pull_request(
                    "session-1", repository, head="fix/x", base="main", title="Fix x", body="Details"
                )
            )

    def test_opens_pull_request_on_selected_repository_with_user_token(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(201, json={"number": 5, "html_url": "https://github.com/acme/api/pull/5"})

        pr = self._open(handler)
        self.assertEqual(seen["path"], "/repos/acme/api/pulls")
        self.assertEqual(seen["body"], {"title": "Fix x", "head": "fix/x", "base": "main", "body": "Details"})
        self.assertEqual(seen["auth"], "Bearer token-123")
        self.assertEqual(pr, {"number": 5, "url": "https://github.com/acme/api/pull/5"})

    def test_github_validation_message_is_surfaced(self):
        def handler(request):
            return httpx.Response(
                422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
            )

        with self.assertRaisesRegex(GitHubConnectionError, "A pull request already exists"):
            self._open(handler)

    def test_unselected_repository_is_refused_before_any_request(self):
        def handler(request):
            raise AssertionError("GitHub must not be called for an unselected repository")

        with self.assertRaises(GitHubConnectionError):
            self._open(handler, repository="acme/secret")


class SessionCookieTests(unittest.TestCase):
    def test_successful_callback_sets_browser_session_cookie(self):
        from fastapi.testclient import TestClient

        from backend.main import app

        session_id = "a" * 32
        with patch("backend.main.complete_github_connection", return_value=session_id):
            response = TestClient(app).get(
                "/auth/github/callback", params={"code": "code", "state": "state"}, follow_redirects=False
            )

        self.assertEqual(response.status_code, 307)
        cookie = response.headers["set-cookie"]
        self.assertIn(f"troubleshooting_session={session_id}", cookie)
        self.assertIn("httponly", cookie.lower())

    def test_invalid_session_id_never_sets_cookie(self):
        from backend.session_cookie import redirect_with_session

        response = redirect_with_session("http://localhost:8501", "bad id; Path=/")
        self.assertNotIn("set-cookie", response.headers)


class PersistedConnectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            patch.object(auth_github, "GITHUB_STATE_DIR", base),
            patch.object(auth_github, "GITHUB_KEY_FILE", base / "store.key"),
            patch.object(auth_github, "GITHUB_STORE_FILE", base / "connections.enc"),
        ]
        for p in self._patches:
            p.start()
        self.store_file = base / "connections.enc"
        auth_github.GITHUB_SESSION_STORE.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        auth_github.GITHUB_SESSION_STORE.clear()
        self._tmp.cleanup()

    def test_connection_survives_reload_and_token_is_encrypted(self):
        repo = auth_github.GitHubRepository(name="app", full_name=f"{ORG}/app", private=True, html_url="https://x")
        auth_github.GITHUB_SESSION_STORE["sess"] = auth_github._GitHubConnection(
            access_token="ghp_secret_token", login="alice", repositories=[repo], selected=[f"{ORG}/app"]
        )
        auth_github.save_github_store()

        self.assertNotIn(b"ghp_secret_token", self.store_file.read_bytes())

        auth_github.GITHUB_SESSION_STORE.clear()
        auth_github._load_github_store()

        status = get_github_status("sess")
        self.assertTrue(status.connected)
        self.assertEqual(status.login, "alice")
        self.assertEqual(status.selected_repositories, [f"{ORG}/app"])
        self.assertEqual(status.repositories[0].full_name, f"{ORG}/app")

    def test_unreadable_store_is_ignored_instead_of_crashing(self):
        self.store_file.parent.mkdir(parents=True, exist_ok=True)
        auth_github._fernet()
        self.store_file.write_bytes(b"not a valid token")
        auth_github._load_github_store()
        self.assertEqual(auth_github.GITHUB_SESSION_STORE, {})


if __name__ == "__main__":
    unittest.main()
