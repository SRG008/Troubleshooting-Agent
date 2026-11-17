import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend import github_tools, github_workspace
from backend.auth_github import GITHUB_CLONE_REGISTRY, GITHUB_SESSION_STORE, GitHubConnectionError, _GitHubConnection


class ResolveInsideTests(unittest.TestCase):
    def test_rejects_traversal_absolute_and_git_paths(self):
        checkout = Path(tempfile.mkdtemp()).resolve()
        for bad in ["../secret.txt", "a/../../secret.txt", "/etc/passwd", "C:/Windows/win.ini", ".git/config", ""]:
            with self.subTest(path=bad), self.assertRaises(GitHubConnectionError):
                github_workspace.resolve_inside(checkout, bad)

    def test_accepts_nested_relative_path(self):
        checkout = Path(tempfile.mkdtemp()).resolve()
        resolved = github_workspace.resolve_inside(checkout, "src/app/main.py")
        self.assertEqual(resolved, checkout / "src" / "app" / "main.py")


class LocalFolderTests(unittest.TestCase):
    def setUp(self):
        # Keeps test connections out of the real encrypted store in data/github/.
        save_patch = patch.object(github_workspace, "save_github_store")
        save_patch.start()
        self.addCleanup(save_patch.stop)
        GITHUB_SESSION_STORE.clear()
        GITHUB_CLONE_REGISTRY.clear()
        GITHUB_SESSION_STORE["session-1"] = _GitHubConnection(
            access_token="token", login="alice", user_id=7, selected=["acme/api"]
        )

    def test_previous_clone_from_an_earlier_chat_is_reused_without_cloning(self):
        clone = Path(tempfile.mkdtemp()).resolve() / "api"
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/acme/api.git"], check=True
        )
        GITHUB_CLONE_REGISTRY["acme/api"] = str(clone)
        empty_parent = Path(tempfile.mkdtemp()).resolve()
        with patch.object(github_workspace, "_clone") as clone_mock:
            path = asyncio.run(github_workspace.set_local_folder("session-1", "acme/api", str(empty_parent)))
        self.assertEqual(path, clone)
        clone_mock.assert_not_called()

    def test_find_previous_clone_is_none_when_never_cloned(self):
        self.assertIsNone(asyncio.run(github_workspace.find_previous_clone("acme/api")))

    def test_finds_clone_beside_another_registered_clone(self):
        parent = Path(tempfile.mkdtemp()).resolve()
        other = parent / "other"
        clone = parent / "api"
        for folder, repo in ((other, "acme/other"), (clone, "acme/api")):
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
            subprocess.run(
                ["git", "-C", str(folder), "remote", "add", "origin", f"https://github.com/{repo}.git"], check=True
            )
        GITHUB_CLONE_REGISTRY["acme/other"] = str(other)
        self.assertEqual(asyncio.run(github_workspace.find_previous_clone("acme/api")), clone)

    def test_missing_saved_checkout_is_reported_instead_of_used(self):
        GITHUB_SESSION_STORE["session-1"].checkouts["acme/api"] = str(Path(tempfile.mkdtemp()) / "gone")
        with self.assertRaises(GitHubConnectionError):
            asyncio.run(github_workspace.ensure_checkout("session-1", "acme/api"))

    def test_refuses_to_guess_a_folder_when_none_is_set(self):
        with self.assertRaisesRegex(GitHubConnectionError, "No local clone of acme/api is available"):
            asyncio.run(github_workspace.ensure_checkout("session-1", "acme/api"))

    def test_parent_folder_with_existing_clone_resolves_to_that_clone(self):
        parent = Path(tempfile.mkdtemp()).resolve()
        clone = parent / "api"
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/acme/api.git"], check=True
        )
        path = asyncio.run(github_workspace.set_local_folder("session-1", "acme/api", str(parent)))
        self.assertEqual(path, clone)
        self.assertEqual(asyncio.run(github_workspace.ensure_checkout("session-1", "acme/api")), clone)

    def test_existing_clone_in_chosen_parent_is_reused_without_cloning(self):
        parent = Path(tempfile.mkdtemp()).resolve()
        clone = parent / "NexEdge"
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/acme/api.git"], check=True
        )
        with patch.object(github_workspace, "_clone") as clone_mock:
            path = asyncio.run(github_workspace.set_local_folder("session-1", "acme/api", str(parent)))
        self.assertEqual(path, clone)
        clone_mock.assert_not_called()

    def test_known_checkout_is_reused_and_never_cloned_again(self):
        known = Path(tempfile.mkdtemp()).resolve() / "api"
        subprocess.run(["git", "init", "-q", str(known)], check=True)
        subprocess.run(
            ["git", "-C", str(known), "remote", "add", "origin", "https://github.com/acme/api.git"], check=True
        )
        GITHUB_SESSION_STORE["session-1"].checkouts["acme/api"] = str(known)
        empty_parent = Path(tempfile.mkdtemp()).resolve()
        with patch.object(github_workspace, "_clone") as clone_mock:
            path = asyncio.run(github_workspace.set_local_folder("session-1", "acme/api", str(empty_parent)))
        self.assertEqual(path, known)
        clone_mock.assert_not_called()

    def test_failed_clone_leaves_no_partial_folder_and_reports_the_error(self):
        parent = Path(tempfile.mkdtemp()).resolve()

        async def failing_git(args, cwd=None, token=None):
            raise GitHubConnectionError("git clone failed: Repository not found")

        with patch.object(github_workspace, "_git", side_effect=failing_git):
            with self.assertRaisesRegex(GitHubConnectionError, "Repository not found"):
                asyncio.run(github_workspace.set_local_folder("session-1", "acme/api", str(parent)))
        self.assertFalse((parent / "api").exists())
        self.assertNotIn("acme/api", GITHUB_SESSION_STORE["session-1"].checkouts)


class ListRepositoriesTests(unittest.TestCase):
    def test_reports_each_selected_repo_with_its_local_folder_or_none(self):
        GITHUB_SESSION_STORE.clear()
        GITHUB_SESSION_STORE["session-2"] = _GitHubConnection(
            access_token="token", login="alice", user_id=7, selected=["acme/api", "acme/web"],
            checkouts={"acme/api": "C:/work/api"},
        )
        handlers = github_tools.build_github_handlers("session-2")
        result = asyncio.run(handlers["list_repositories"]({}))
        self.assertEqual(
            result["repositories"],
            [
                {"full_name": "acme/api", "local_folder": "C:/work/api"},
                {"full_name": "acme/web", "local_folder": None},
            ],
        )


class DiscardFileTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-q", "-m", "init")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def test_discard_restores_tracked_file_and_removes_new_file(self):
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        asyncio.run(github_workspace.discard_file(self.repo, "tracked.txt"))
        asyncio.run(github_workspace.discard_file(self.repo, "new.txt"))
        self.assertEqual((self.repo / "tracked.txt").read_text(encoding="utf-8"), "original\n")
        self.assertFalse((self.repo / "new.txt").exists())


class ReviewDiffTests(unittest.TestCase):
    def setUp(self):
        GITHUB_SESSION_STORE.clear()
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "T"], check=True)
        (self.repo / "Program.cs").write_text("class A {\n    void M() {}\n}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "init"], check=True)
        (self.repo / "Program.cs").write_text("class A {\n    void M() { Run(); }\n}\n", encoding="utf-8")
        GITHUB_SESSION_STORE["session-1"] = _GitHubConnection(
            access_token="token", login="alice", user_id=7, selected=["acme/api"],
            checkouts={"acme/api": str(self.repo)},
        )

    def tearDown(self):
        GITHUB_SESSION_STORE.clear()

    def test_question_naming_a_changed_file_gets_its_before_and_after_code(self):
        (self.repo / "New.cs").write_text("class New {}\n", encoding="utf-8")
        changes = asyncio.run(github_workspace.repo_changes("session-1", "acme/api"))
        self.assertEqual({c["path"]: c["status"] for c in changes}, {"Program.cs": "modified", "New.cs": "added"})
        review = github_workspace.review_for_question("session-1", "Keep or discard Program.cs?")
        self.assertEqual(review["old"], "class A {\n    void M() {}\n}\n")
        self.assertIn("void M() { Run(); }", review["new"])
        self.assertEqual(review["language"], "csharp")
        self.assertIsNone(github_workspace.review_for_question("session-1", "What is the app name?"))
        self.assertIsNone(github_workspace.review_for_question("session-1", "Keep or discard New.cs?")["old"])


class RepoFilesFromGitHubTests(unittest.TestCase):
    def setUp(self):
        GITHUB_SESSION_STORE.clear()
        GITHUB_SESSION_STORE["session-1"] = _GitHubConnection(
            access_token="token", login="alice", user_id=7, selected=["acme/api"]
        )

    def tearDown(self):
        GITHUB_SESSION_STORE.clear()

    def test_counts_and_filters_default_branch_files_without_a_clone(self):
        def responses():
            return [
                httpx.Response(200, json={"default_branch": "main"}),
                httpx.Response(
                    200,
                    json={
                        "truncated": False,
                        "tree": [
                            {"path": "README.md", "type": "blob"},
                            {"path": "src", "type": "tree"},
                            {"path": "src/app.py", "type": "blob"},
                        ],
                    },
                ),
            ]

        with patch.object(github_workspace, "_github_request", side_effect=responses()):
            everything = asyncio.run(github_workspace.list_repo_files("session-1", "acme/api"))
        self.assertEqual(everything["branch"], "main")
        self.assertEqual(everything["total_files"], 2)
        self.assertEqual(everything["files"], ["README.md", "src/app.py"])

        with patch.object(github_workspace, "_github_request", side_effect=responses()):
            under_src = asyncio.run(github_workspace.list_repo_files("session-1", "acme/api", "src"))
        self.assertEqual(under_src["files"], ["src/app.py"])

    def test_lists_the_given_branch_without_asking_for_the_default(self):
        tree = httpx.Response(200, json={"tree": [{"path": "dev.txt", "type": "blob"}]})
        with patch.object(github_workspace, "_github_request", return_value=tree) as request:
            result = asyncio.run(github_workspace.list_repo_files("session-1", "acme/api", branch="feature/x"))
        self.assertEqual(result["branch"], "feature/x")
        self.assertEqual(result["files"], ["dev.txt"])
        self.assertEqual(request.call_count, 1)

    def test_rejected_token_drops_the_connection_and_asks_to_reconnect(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
        with patch.object(github_workspace, "_http_client", return_value=client), patch.object(
            github_workspace, "save_github_store"
        ):
            with self.assertRaisesRegex(GitHubConnectionError, "Connect GitHub again"):
                asyncio.run(github_workspace.list_repo_files("session-1", "acme/api"))
        self.assertNotIn("session-1", GITHUB_SESSION_STORE)

    def test_search_refuses_a_branch_other_than_the_default(self):
        default = httpx.Response(200, json={"default_branch": "main"})
        with patch.object(github_workspace, "_github_request", return_value=default):
            with self.assertRaises(GitHubConnectionError):
                asyncio.run(github_workspace.search_repo_code("session-1", "acme/api", "login", branch="dev"))


class RepoHistoryFromGitHubTests(unittest.TestCase):
    def test_counts_commits_from_the_last_page_and_lists_latest(self):
        latest = httpx.Response(
            200,
            json=[
                {
                    "sha": "abcdef123456",
                    "commit": {
                        "author": {"name": "Ann", "date": "2026-01-02T10:00:00Z"},
                        "message": "Fix login\n\nlonger body",
                    },
                }
            ],
        )
        counted = httpx.Response(
            200,
            json=[{"sha": "abcdef123456"}],
            headers={"Link": '<https://api.github.com/repositories/1/commits?per_page=1&page=2>; rel="next", '
                             '<https://api.github.com/repositories/1/commits?per_page=1&page=42>; rel="last"'},
        )
        with patch.object(github_workspace, "_github_request", side_effect=[latest, counted]):
            result = asyncio.run(github_workspace.repo_history("session", "acme/api", limit=1))
        self.assertEqual(result["commit_count"], 42)
        self.assertEqual(
            result["recent_commits"], [{"sha": "abcdef1", "author": "Ann", "date": "2026-01-02", "message": "Fix login"}]
        )


class CommitAndPushTests(unittest.TestCase):
    """Runs commit_and_push against a local bare repository standing in for GitHub."""

    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.remote = root / "remote.git"
        seed = root / "seed"
        self.checkout = root / "checkout"
        self._git(root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self._git(root, "init", "-q", "-b", "main", str(seed))
        self._git(seed, "config", "user.email", "seed@example.com")
        self._git(seed, "config", "user.name", "Seed")
        (seed / "app.py").write_text("print('broken')\n", encoding="utf-8")
        self._git(seed, "add", "app.py")
        self._git(seed, "commit", "-q", "-m", "init")
        self._git(seed, "push", "-q", str(self.remote), "main")
        self._git(root, "clone", "-q", str(self.remote), str(self.checkout))

        GITHUB_SESSION_STORE.clear()
        GITHUB_SESSION_STORE["session-1"] = _GitHubConnection(
            access_token="token-123",
            login="alice",
            user_id=7,
            selected=["acme/api"],
            checkouts={"acme/api": str(self.checkout)},
        )

    def _git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout

    def test_commits_as_the_user_on_a_new_branch_and_pushes_it(self):
        (self.checkout / "app.py").write_text("print('fixed')\n", encoding="utf-8")
        sha = asyncio.run(
            github_workspace.commit_and_push(
                "session-1", "acme/api", branch="fix/KAN-1-login", base="main",
                message="Fix login", author_login="alice", author_id=7,
            )
        )
        self.assertEqual(len(sha), 40)
        remote_heads = self._git(self.checkout, "ls-remote", "--heads", str(self.remote), "fix/KAN-1-login")
        self.assertIn("refs/heads/fix/KAN-1-login", remote_heads)
        self._git(self.checkout, "fetch", "-q", "origin")
        author = self._git(self.checkout, "log", "-1", "--format=%an <%ae>", "origin/fix/KAN-1-login").strip()
        self.assertEqual(author, "alice <7+alice@users.noreply.github.com>")

    def test_commits_on_a_branch_created_earlier_without_recreating_it(self):
        asyncio.run(github_workspace.create_branch("session-1", "acme/api", "feat/NEX-2-health"))
        (self.checkout / "app.py").write_text("print('health')\n", encoding="utf-8")
        asyncio.run(
            github_workspace.commit_and_push(
                "session-1", "acme/api", branch="feat/NEX-2-health", base="main",
                message="Add health", author_login="alice", author_id=7,
            )
        )
        self.assertEqual(self._git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD").strip(), "feat/NEX-2-health")
        remote_heads = self._git(self.checkout, "ls-remote", "--heads", str(self.remote), "feat/NEX-2-health")
        self.assertIn("refs/heads/feat/NEX-2-health", remote_heads)

    def test_refuses_to_commit_when_nothing_changed(self):
        with self.assertRaises(GitHubConnectionError):
            asyncio.run(
                github_workspace.commit_and_push(
                    "session-1", "acme/api", branch="fix/none", base="main",
                    message="Nothing", author_login="alice", author_id=7,
                )
            )

    def test_rejects_unsafe_branch_names(self):
        for bad in ["--upload-pack=evil", "fix/../main", "-x", "feature.lock", "trailing/"]:
            with self.subTest(branch=bad), self.assertRaises(GitHubConnectionError):
                asyncio.run(
                    github_workspace.commit_and_push(
                        "session-1", "acme/api", branch=bad, base="main",
                        message="x", author_login="alice", author_id=7,
                    )
                )


if __name__ == "__main__":
    unittest.main()
