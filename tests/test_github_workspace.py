import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import github_tools, github_workspace
from backend.auth_github import GITHUB_SESSION_STORE, GitHubConnectionError, _GitHubConnection


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


class ListFilesTests(unittest.TestCase):
    def test_lists_tracked_files_under_a_directory_only(self):
        repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "src").mkdir()
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        (repo / "src" / "app.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        everything = asyncio.run(github_workspace.list_files(repo))
        self.assertEqual(everything.splitlines(), ["README.md", "src/app.py"])
        under_src = asyncio.run(github_workspace.list_files(repo, "src"))
        self.assertEqual(under_src.splitlines(), ["src/app.py"])


class LocalFolderTests(unittest.TestCase):
    def setUp(self):
        # Keeps test connections out of the real encrypted store in data/github/.
        save_patch = patch.object(github_workspace, "save_github_store")
        save_patch.start()
        self.addCleanup(save_patch.stop)
        GITHUB_SESSION_STORE.clear()
        GITHUB_SESSION_STORE["session-1"] = _GitHubConnection(
            access_token="token", login="alice", user_id=7, selected=["acme/api"]
        )

    def test_refuses_to_guess_a_folder_when_none_is_set(self):
        with self.assertRaisesRegex(GitHubConnectionError, "No local folder is set for acme/api"):
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


class RepoHistoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        for number in range(3):
            (self.repo / "file.txt").write_text(f"{number}\n", encoding="utf-8")
            self._git("add", "file.txt")
            self._git("commit", "-q", "-m", f"commit {number}")
        self._git("update-ref", "refs/remotes/origin/main", "HEAD")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def test_counts_commits_of_remote_branch_and_lists_latest(self):
        original_git = github_workspace._git

        async def git_without_network(args, cwd=None, token=None):
            if args[:1] == ["fetch"]:
                return ""
            return await original_git(args, cwd=cwd, token=token)

        async def checkout(session_id, full_name):
            return self.repo

        connection = _GitHubConnection(access_token="token", login="octo", user_id=1, repositories=[])
        with patch.object(github_workspace, "require_selected_repository", return_value=connection), patch.object(
            github_workspace, "ensure_checkout", side_effect=checkout
        ), patch.object(github_workspace, "_git", side_effect=git_without_network):
            result = asyncio.run(github_workspace.repo_history("session", "octo/repo", branch="main", limit=2))
        self.assertEqual(result["commit_count"], 3)
        self.assertEqual([c["message"] for c in result["recent_commits"]], ["commit 2", "commit 1"])


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
