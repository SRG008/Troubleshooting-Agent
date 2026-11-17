"""Local checkouts of GitHub repositories that the agent reads and changes.

Tokens are passed to git through environment variables so they never appear in argv,
the remote URL, or .git/config.
"""

import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx

from backend.auth_github import (
    GITHUB_API_URL,
    GITHUB_CLONE_REGISTRY,
    GitHubConnectionError,
    _describe_api_error,
    _http_client,
    drop_rejected_connection,
    require_selected_repository,
    save_github_store,
)

logger = logging.getLogger(__name__)
MAX_LISTED_FILES = 500
GIT_TIMEOUT_SECONDS = 120
MAX_FILE_CHARS = 60_000
BRANCH_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_clone_locks: Dict[str, asyncio.Lock] = {}


def _repo_folder_name(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    if not owner or not name or "/" in name:
        raise GitHubConnectionError(f"Invalid repository name: {full_name}")
    return name


def pick_folder() -> str:
    """Opens the folder picker on this machine. Returns the chosen path, or an empty string if cancelled."""
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory(title='Choose the folder for the repository'), end='')\n"
        "root.destroy()\n"
    )
    try:
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise GitHubConnectionError("The folder picker timed out.")
    if result.returncode != 0:
        raise GitHubConnectionError(f"Could not open the folder picker: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def resolve_inside(checkout: Path, relative: str) -> Path:
    """Returns the absolute path for a repo-relative file, refusing anything that escapes the checkout."""
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
        raise GitHubConnectionError(f"Invalid file path: {relative}")
    resolved = (checkout / candidate).resolve()
    if checkout.resolve() not in resolved.parents:
        raise GitHubConnectionError(f"Invalid file path: {relative}")
    return resolved


async def _git(args: list, cwd: Optional[Path] = None, token: Optional[str] = None) -> str:
    # Runs git in a worker thread: asyncio subprocesses are unsupported on Windows' selector loop.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
        )
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise GitHubConnectionError(f"git {args[0]} timed out.")
    if completed.returncode != 0:
        message = completed.stderr.decode(errors="replace").strip()
        raise GitHubConnectionError(f"git {args[0]} failed: {message[:500]}")
    return completed.stdout.decode(errors="replace")



def _normalize_remote(url: str) -> str:
    value = url.strip().lower().removesuffix("/").removesuffix(".git")
    value = re.sub(r"^(https?://|ssh://)?([^@/]+@)?", "", value)
    return value.replace("github.com:", "github.com/")


async def _origin_matches(checkout: Path, full_name: str) -> bool:
    """True when the folder is the top level of a git clone whose origin is the given GitHub repository."""
    try:
        toplevel = (await _git(["rev-parse", "--show-toplevel"], cwd=checkout)).strip()
        origin = (await _git(["remote", "get-url", "origin"], cwd=checkout)).strip()
    except GitHubConnectionError:
        return False
    if Path(toplevel).resolve() != checkout.resolve():
        return False
    return _normalize_remote(origin).endswith(f"github.com/{full_name.lower()}")


async def _clone(connection, full_name: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{full_name}.git"
    try:
        await _git(["clone", "--quiet", url, str(path)], token=connection.access_token)
    except GitHubConnectionError as error:
        logger.error("Cloning %s failed: %s", full_name, error)
        shutil.rmtree(path, ignore_errors=True)
        raise


async def _find_existing_clone(full_name: str, folder: Path) -> Optional[Path]:
    """The folder itself or one of its immediate subfolders, when it is a clone of the repository."""
    if await _origin_matches(folder, full_name):
        return folder
    for child in sorted(folder.iterdir()):
        if child.is_dir() and await _origin_matches(child, full_name):
            return child
    return None


async def find_previous_clone(full_name: str) -> Optional[Path]:
    """The folder where this repository was cloned before, if it is still there.

    Checks the registered clone first, then the folders next to every other registered clone, so a
    clone made before the registry existed is still found when it sits beside a known one.
    """
    known = GITHUB_CLONE_REGISTRY.get(full_name)
    if known and Path(known).is_dir() and await _origin_matches(Path(known), full_name):
        return Path(known)
    parents = {Path(path).parent for path in GITHUB_CLONE_REGISTRY.values() if path}
    for parent in sorted(parents):
        if parent.is_dir():
            found = await _find_existing_clone(full_name, parent)
            if found:
                return found
    return None


async def set_local_folder(session_id: str, full_name: str, folder: str) -> Path:
    """Sets where a selected repository lives on this machine.

    The folder is either an existing clone of the repository, or a parent folder the repository
    is cloned into (as a subfolder named after the repository). An existing clone is reused,
    never cloned again.
    """
    connection = require_selected_repository(session_id, full_name)
    chosen = Path(folder).expanduser().resolve()
    if not chosen.is_dir():
        raise GitHubConnectionError(f"Folder not found: {folder}")
    async with _clone_locks.setdefault(full_name, asyncio.Lock()):
        path = await find_previous_clone(full_name)
        known = connection.checkouts.get(full_name)
        if path is None and known and Path(known).is_dir():
            path = await _find_existing_clone(full_name, Path(known))
        path = path or await _find_existing_clone(full_name, chosen)
        if path is None:
            path = chosen / _repo_folder_name(full_name)
            if path.exists():
                raise GitHubConnectionError(f"{path} exists but is not a clone of {full_name}.")
            await _clone(connection, full_name, path)
        connection.checkouts[full_name] = str(path)
        GITHUB_CLONE_REGISTRY[full_name] = str(path)
        save_github_store()
        return path


async def ensure_checkout(session_id: str, full_name: str) -> Path:
    """Returns the local folder of a selected repository. The user must set it first."""
    connection = require_selected_repository(session_id, full_name)
    for candidate in (connection.checkouts.get(full_name), GITHUB_CLONE_REGISTRY.get(full_name)):
        if candidate and Path(candidate).is_dir():
            connection.checkouts[full_name] = candidate
            return Path(candidate)
    raise GitHubConnectionError(
        f"No local clone of {full_name} is available. Its saved folder is missing or is no longer a clone. "
        "Ask the user in chat whether it is already cloned, then ask for the folder with ask_user and call use_local_clone."
    )


def write_file(checkout: Path, relative: str, content: str) -> None:
    target = resolve_inside(checkout, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


async def _github_request(
    session_id: str,
    full_name: str,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    accept: str = "application/vnd.github+json",
) -> httpx.Response:
    """A GET to the GitHub API with the session's token, for a selected repository. No clone needed."""
    connection = require_selected_repository(session_id, full_name)
    async with _http_client() as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {connection.access_token}", "Accept": accept},
        )
    if resp.status_code == 401:
        drop_rejected_connection(session_id)
    return resp


def _repo_url(full_name: str, path: str = "") -> str:
    return f"{GITHUB_API_URL}/repos/{full_name}{path}"


def _checked(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code == 404:
        raise GitHubConnectionError(f"Not found on GitHub: {what}")
    if resp.status_code != 200:
        raise GitHubConnectionError(_describe_api_error(resp))
    return resp


def _validate_relative(relative: str) -> None:
    candidate = PurePosixPath(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
        raise GitHubConnectionError(f"Invalid file path: {relative}")


async def _default_branch(session_id: str, full_name: str) -> str:
    repo = _checked(await _github_request(session_id, full_name, _repo_url(full_name)), full_name)
    return repo.json()["default_branch"]


async def list_repo_files(session_id: str, full_name: str, directory: str = "", branch: str = "") -> Dict[str, Any]:
    """Every file of a branch (default branch when none is given) on GitHub, so counts and names are exact without a clone."""
    if branch:
        _validate_branch_name(branch)
    else:
        branch = await _default_branch(session_id, full_name)
    tree = _checked(
        await _github_request(session_id, full_name, _repo_url(full_name, f"/git/trees/{quote(branch, safe='')}"), {"recursive": 1}),
        branch,
    )
    body = tree.json()
    prefix = ""
    if directory:
        _validate_relative(directory)
        prefix = directory.strip("/") + "/"
    files = sorted(
        item["path"] for item in body.get("tree", []) if item["type"] == "blob" and item["path"].startswith(prefix)
    )
    return {
        "branch": branch,
        "total_files": len(files),
        "files": files[:MAX_LISTED_FILES],
        "truncated": bool(body.get("truncated")) or len(files) > MAX_LISTED_FILES,
    }


async def read_repo_file(session_id: str, full_name: str, relative: str, branch: str = "") -> str:
    _validate_relative(relative)
    params: Dict[str, Any] = {}
    if branch:
        _validate_branch_name(branch)
        params["ref"] = branch
    resp = await _github_request(
        session_id,
        full_name,
        _repo_url(full_name, f"/contents/{quote(relative, safe='/')}"),
        params,
        accept="application/vnd.github.raw+json",
    )
    return _checked(resp, relative).text[:MAX_FILE_CHARS]


async def search_repo_code(session_id: str, full_name: str, pattern: str, branch: str = "") -> Dict[str, Any]:
    """GitHub code search for a keyword in one repository. Returns matching file paths.

    GitHub's code search only indexes the default branch, so another branch is refused rather than searched wrongly.
    """
    if not pattern.strip():
        raise GitHubConnectionError("Search pattern must not be empty.")
    if branch:
        _validate_branch_name(branch)
        default = await _default_branch(session_id, full_name)
        if branch != default:
            raise GitHubConnectionError(
                f"GitHub code search only covers the default branch ({default}). "
                f"For {branch}, use list_repo_files and get_repo_file instead."
            )
    resp = await _github_request(
        session_id,
        full_name,
        f"{GITHUB_API_URL}/search/code",
        {"q": f"{pattern} repo:{full_name}", "per_page": 30},
    )
    body = _checked(resp, "code search").json()
    return {"total_count": body.get("total_count", 0), "matches": [item["path"] for item in body.get("items", [])]}



async def repo_history(session_id: str, full_name: str, branch: str = "", limit: int = 10) -> Dict[str, Any]:
    """Commit count and latest commits of a branch (default branch when none is given), straight from GitHub."""
    limit = min(max(int(limit or 10), 1), 50)
    branch_params: Dict[str, Any] = {}
    if branch:
        _validate_branch_name(branch)
        branch_params["sha"] = branch
    commits_url = _repo_url(full_name, "/commits")
    latest = _checked(
        await _github_request(session_id, full_name, commits_url, {**branch_params, "per_page": limit}), full_name
    ).json()
    counted = _checked(
        await _github_request(session_id, full_name, commits_url, {**branch_params, "per_page": 1}), full_name
    )
    return {
        "ref": branch or "default branch",
        "commit_count": _page_count(counted),
        "recent_commits": [
            {
                "sha": item["sha"][:7],
                "author": item["commit"]["author"]["name"],
                "date": item["commit"]["author"]["date"][:10],
                "message": item["commit"]["message"].splitlines()[0],
            }
            for item in latest
        ],
    }


def _page_count(resp: httpx.Response) -> int:
    """Commits are counted from the last page of a one-per-page listing, the same way GitHub pages them."""
    last = resp.links.get("last")
    if not last:
        return len(resp.json())
    return int(parse_qs(urlparse(last["url"]).query)["page"][0])


LANGUAGE_BY_SUFFIX = {
    ".cs": "csharp", ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".go": "go", ".rs": "rust", ".json": "json", ".md": "markdown", ".html": "html",
    ".css": "css", ".yml": "yaml", ".yaml": "yaml", ".sql": "sql", ".xml": "xml", ".sh": "bash",
}
_last_reviews: Dict[str, Dict[str, Dict[str, Any]]] = {}


async def repo_changes(session_id: str, full_name: str) -> List[Dict[str, Any]]:
    """Every file changed in the local checkout, with its status and the text before and after the change."""
    checkout = await ensure_checkout(session_id, full_name)
    # Intent-to-add makes new files show up as added in the status listing.
    await _git(["add", "--intent-to-add", "--all"], cwd=checkout)
    reviews = {}
    for line in (await _git(["diff", "--name-status"], cwd=checkout)).splitlines():
        status, _, name = line.partition("\t")
        if not name:
            continue
        status = {"A": "added", "D": "deleted"}.get(status[:1], "modified")
        old = None if status == "added" else await _git(["show", f"HEAD:{name}"], cwd=checkout)
        target = checkout / name
        new = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        reviews[name] = {
            "path": name,
            "status": status,
            "old": old,
            "new": new,
            "language": LANGUAGE_BY_SUFFIX.get(PurePosixPath(name).suffix.lower(), "text"),
        }
    _last_reviews[session_id] = reviews
    return list(reviews.values())


def review_for_question(session_id: str, question: str) -> Optional[Dict[str, Any]]:
    """The latest review of the changed file a question names, if any."""
    for path, review in _last_reviews.get(session_id, {}).items():
        if path in question:
            return review
    return None


def _validate_branch_name(name: str) -> None:
    if not BRANCH_NAME_PATTERN.fullmatch(name) or ".." in name or name.endswith((".lock", "/", ".")):
        raise GitHubConnectionError(f"Invalid branch name: {name}")


async def create_branch(session_id: str, full_name: str, branch: str) -> str:
    """Creates a local branch from the current HEAD and switches to it. Uncommitted changes are kept."""
    _validate_branch_name(branch)
    checkout = await ensure_checkout(session_id, full_name)
    await _git(["checkout", "-b", branch], cwd=checkout)
    return branch


async def commit_and_push(
    session_id: str, full_name: str, branch: str, base: str, message: str, author_login: str, author_id: int
) -> str:
    """Commits every remaining local change on <branch> and pushes it.

    If <branch> is not checked out yet, it is created from origin/<base> first. Returns the commit SHA.
    Uses the user's GitHub identity as commit author.
    """
    connection = require_selected_repository(session_id, full_name)
    _validate_branch_name(branch)
    _validate_branch_name(base)
    checkout = await ensure_checkout(session_id, full_name)
    token = connection.access_token

    if not (await _git(["status", "--porcelain"], cwd=checkout)).strip():
        raise GitHubConnectionError("There are no changes to commit.")
    await _git(["fetch", "--quiet", "origin", base], cwd=checkout, token=token)
    if await current_branch(checkout) != branch:
        await _git(["checkout", "-b", branch, f"origin/{base}"], cwd=checkout)
    await _git(["add", "--all"], cwd=checkout)
    email = f"{author_id}+{author_login}@users.noreply.github.com"
    await _git(
        ["-c", f"user.name={author_login}", "-c", f"user.email={email}", "commit", "--quiet", "-m", message],
        cwd=checkout,
    )
    await _git(["push", "--quiet", "-u", "origin", branch], cwd=checkout, token=token)
    return (await _git(["rev-parse", "HEAD"], cwd=checkout)).strip()


async def discard_file(checkout: Path, relative: str) -> None:
    """Reverts one file to HEAD. Files that are not in HEAD are removed."""
    target = resolve_inside(checkout, relative)
    try:
        await _git(["cat-file", "-e", f"HEAD:{relative}"], cwd=checkout)
        in_head = True
    except GitHubConnectionError:
        in_head = False
    if in_head:
        await _git(["checkout", "HEAD", "--", relative], cwd=checkout)
    elif target.is_file():
        target.unlink()
    await _git(["reset", "-q", "--", relative], cwd=checkout)


async def current_branch(checkout: Path) -> str:
    return (await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout)).strip()
