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
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.auth_github import GitHubConnectionError, require_selected_repository, save_github_store

logger = logging.getLogger(__name__)
MAX_LISTED_FILES = 500
GIT_TIMEOUT_SECONDS = 120
MAX_FILE_CHARS = 60_000
MAX_DIFF_CHARS = 4000
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


async def set_local_folder(session_id: str, full_name: str, folder: str) -> Path:
    """Sets where a selected repository lives on this machine.

    The folder is either an existing clone of the repository, or a parent folder the repository
    is cloned into (as a subfolder named after the repository).
    """
    connection = require_selected_repository(session_id, full_name)
    chosen = Path(folder).expanduser().resolve()
    if not chosen.is_dir():
        raise GitHubConnectionError(f"Folder not found: {folder}")
    async with _clone_locks.setdefault(full_name, asyncio.Lock()):
        if await _origin_matches(chosen, full_name):
            path = chosen
        else:
            path = chosen / _repo_folder_name(full_name)
            if path.exists():
                if not await _origin_matches(path, full_name):
                    raise GitHubConnectionError(f"{path} exists but is not a clone of {full_name}.")
            else:
                await _clone(connection, full_name, path)
        connection.checkouts[full_name] = str(path)
        save_github_store()
        return path


async def ensure_checkout(session_id: str, full_name: str) -> Path:
    """Returns the local folder of a selected repository. The user must set it first."""
    connection = require_selected_repository(session_id, full_name)
    known = connection.checkouts.get(full_name)
    if not known:
        raise GitHubConnectionError(
            f"No local folder is set for {full_name}. Ask the user in chat whether it is already cloned, "
            "then ask for the folder with ask_user and call use_local_clone."
        )
    return Path(known)


def read_file(checkout: Path, relative: str) -> str:
    target = resolve_inside(checkout, relative)
    if not target.is_file():
        raise GitHubConnectionError(f"No such file: {relative}")
    text = target.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_FILE_CHARS]


def write_file(checkout: Path, relative: str, content: str) -> None:
    target = resolve_inside(checkout, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


async def list_files(checkout: Path, directory: str = "") -> str:
    """Tracked files under a directory of the checkout, so the agent reads real names instead of guessing."""
    prefix = ""
    if directory:
        resolve_inside(checkout, directory)
        prefix = directory.rstrip("/") + "/"
    output = await _git(["ls-files", "--", prefix] if prefix else ["ls-files"], cwd=checkout)
    files = [line for line in output.splitlines() if line]
    return "\n".join(files[:MAX_LISTED_FILES]) + (
        f"\n... {len(files) - MAX_LISTED_FILES} more files not shown" if len(files) > MAX_LISTED_FILES else ""
    )


async def search_code(checkout: Path, pattern: str, max_lines: int = 100) -> str:
    if not pattern.strip():
        raise GitHubConnectionError("Search pattern must not be empty.")
    output = await _git(["grep", "-n", "-I", "--no-color", "-e", pattern], cwd=checkout)
    lines = output.splitlines()[:max_lines]
    return "\n".join(lines)


async def repo_history(session_id: str, full_name: str, branch: str = "", limit: int = 10) -> Dict[str, Any]:
    """Commit count and latest commits of a branch (default branch when none is given), as on GitHub."""
    connection = require_selected_repository(session_id, full_name)
    checkout = await ensure_checkout(session_id, full_name)
    # Fetch first so the count matches GitHub rather than the last local pull.
    await _git(["fetch", "--quiet", "--prune", "origin"], cwd=checkout, token=connection.access_token)
    if branch:
        _validate_branch_name(branch)
        ref = f"origin/{branch}"
    else:
        ref = "origin/HEAD"
    limit = min(max(int(limit or 10), 1), 50)
    count = int((await _git(["rev-list", "--count", ref], cwd=checkout)).strip())
    log = await _git(
        ["log", f"-n{limit}", "--date=short", "--format=%h%x09%an%x09%ad%x09%s", ref], cwd=checkout
    )
    commits = [dict(zip(("sha", "author", "date", "message"), line.split("\t", 3))) for line in log.splitlines() if line]
    return {"ref": ref, "commit_count": count, "recent_commits": commits}


async def repo_changes(session_id: str, full_name: str) -> List[Dict[str, str]]:
    """Every file changed in the local checkout, with its diff, so the user can review each one."""
    checkout = await ensure_checkout(session_id, full_name)
    # Intent-to-add makes new files appear in the diff.
    await _git(["add", "--intent-to-add", "--all"], cwd=checkout)
    names = [name for name in (await _git(["diff", "--name-only"], cwd=checkout)).splitlines() if name]
    changes = []
    for name in names:
        diff = await _git(["diff", "--no-color", "--", name], cwd=checkout)
        changes.append({"path": name, "diff": diff[:MAX_DIFF_CHARS]})
    return changes


def _validate_branch_name(name: str) -> None:
    if not BRANCH_NAME_PATTERN.fullmatch(name) or ".." in name or name.endswith((".lock", "/", ".")):
        raise GitHubConnectionError(f"Invalid branch name: {name}")


async def commit_and_push(
    session_id: str, full_name: str, branch: str, base: str, message: str, author_login: str, author_id: int
) -> str:
    """Creates a branch from origin/<base>, commits every remaining local change, and pushes the branch.

    Returns the commit SHA. Uses the user's GitHub identity as commit author.
    """
    connection = require_selected_repository(session_id, full_name)
    _validate_branch_name(branch)
    _validate_branch_name(base)
    checkout = await ensure_checkout(session_id, full_name)
    token = connection.access_token

    if not (await _git(["status", "--porcelain"], cwd=checkout)).strip():
        raise GitHubConnectionError("There are no changes to commit.")
    await _git(["fetch", "--quiet", "origin", base], cwd=checkout, token=token)
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
