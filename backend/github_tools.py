"""Agent tools that read, change, and open pull requests in the GitHub repositories selected in the sidebar."""

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from backend.auth_github import (
    GitHubConnectionError,
    get_github_connection,
    list_branch_names,
    list_pull_requests,
    open_pull_request,
)
from backend.github_workspace import (
    commit_and_push,
    create_branch,
    discard_file,
    ensure_checkout,
    find_previous_clone,
    list_repo_files,
    read_repo_file,
    repo_changes,
    repo_history,
    search_repo_code,
    set_local_folder,
    write_file,
)
from backend.llm import ToolSpec

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]

_REPOSITORY = {
    "type": "string",
    "description": "Full name owner/name of one selected repository.",
}
_PATH = {"type": "string", "description": "File path relative to the repository root."}

GITHUB_TOOL_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="list_repositories",
        description="List the GitHub repositories the user selected in the sidebar. Only these may be used.",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="use_local_clone",
        description=(
            "Set the local folder of a selected repository. The folder is either an existing clone of it, "
            "or a parent folder to clone the repository into. Call only after the user gave the path."
        ),
        parameters={
            "type": "object",
            "properties": {"repository": _REPOSITORY, "folder": {"type": "string", "description": "Absolute folder path."}},
            "required": ["repository", "folder"],
        },
    ),
    ToolSpec(
        name="find_previous_clone",
        description=(
            "Check whether a selected repository was already cloned on this machine before, in any chat. Returns the "
            "folder, or null. Call it when the user says the repository is not cloned yet, before cloning again."
        ),
        parameters={"type": "object", "properties": {"repository": _REPOSITORY}, "required": ["repository"]},
    ),
    ToolSpec(
        name="list_branches",
        description="List the branches of a selected repository, for choosing the base branch of a pull request.",
        parameters={"type": "object", "properties": {"repository": _REPOSITORY}, "required": ["repository"]},
    ),
    ToolSpec(
        name="list_pull_requests",
        description=(
            "List pull requests of a selected repository from GitHub, with number, title, author, branches and link. "
            "Use it for any question about pull requests. state is open, closed or all (default open)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
            "required": ["repository"],
        },
    ),
    ToolSpec(
        name="list_repo_files",
        description=(
            "List the files of a selected repository on GitHub, optionally under one directory. Uses the default "
            "branch unless a branch is given. Returns branch, total_files, the file names, and whether the list is "
            "truncated. No local clone is needed. Use it for file counts and before naming any file; never guess file names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "directory": {"type": "string", "description": "Optional directory relative to the repository root."},
                "branch": {"type": "string", "description": "Optional branch name. Defaults to the default branch."},
            },
            "required": ["repository"],
        },
    ),
    ToolSpec(
        name="get_repo_file",
        description=(
            "Read a text file of a selected repository from GitHub, on the default branch or the given branch. "
            "No local clone is needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "path": _PATH,
                "branch": {"type": "string", "description": "Optional branch name. Defaults to the default branch."},
            },
            "required": ["repository", "path"],
        },
    ),
    ToolSpec(
        name="search_repo_code",
        description=(
            "Search the code of a selected repository on GitHub for a keyword or phrase. Returns the matching file "
            "paths; read them with get_repo_file to see the lines. Only the default branch is searched. No local clone is needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "pattern": {"type": "string", "description": "Keyword or phrase to search for."},
                "branch": {
                    "type": "string",
                    "description": "Optional branch name. Only the default branch can be searched.",
                },
            },
            "required": ["repository", "pattern"],
        },
    ),
    ToolSpec(
        name="write_repo_file",
        description=(
            "Create or replace a text file in a selected repository with its full new content. Needs a local clone "
            "(see the clone question in the instructions). The change stays local until the user approves a commit."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "path": _PATH,
                "content": {"type": "string", "description": "Full new content of the file."},
            },
            "required": ["repository", "path", "content"],
        },
    ),
    ToolSpec(
        name="get_repo_history",
        description=(
            "Count the commits of a selected repository on its default branch or a given branch, as on GitHub, "
            "and list the latest commits. Use it for any question about commits, history or activity."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "branch": {"type": "string", "description": "Optional branch name. Defaults to the default branch."},
                "limit": {"type": "integer", "description": "How many latest commits to list (1-50, default 10)."},
            },
            "required": ["repository"],
        },
    ),
    ToolSpec(
        name="get_repo_changes",
        description="List every file changed locally in a selected repository, with its status (added, modified, deleted). The user sees each file's code before and after the change when you ask keep or discard.",
        parameters={"type": "object", "properties": {"repository": _REPOSITORY}, "required": ["repository"]},
    ),
    ToolSpec(
        name="discard_repo_file",
        description="Undo all local changes to one file of a selected repository. Use when the user does not keep it.",
        parameters={
            "type": "object",
            "properties": {"repository": _REPOSITORY, "path": _PATH},
            "required": ["repository", "path"],
        },
    ),
    ToolSpec(
        name="create_repo_branch",
        description=(
            "Create a new local branch in a selected repository and switch to it, before any file is changed. "
            "Call only after the user chose to create a branch and gave its name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "branch": {"type": "string", "description": "New branch name, for example fix/KAN-12-login-error."},
            },
            "required": ["repository", "branch"],
        },
    ),
    ToolSpec(
        name="commit_and_open_pr",
        description=(
            "Commit all kept local changes as the user, push the branch, and open a pull request. If the branch is "
            "already checked out (created with create_repo_branch), it is committed on that branch; otherwise it is "
            "created from the chosen base branch. Call only after the user explicitly confirmed the commit and chose "
            "the base branch."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": _REPOSITORY,
                "branch": {"type": "string", "description": "Branch name, the same as the one created earlier if any."},
                "base": {"type": "string", "description": "Base branch the pull request targets."},
                "commit_message": {"type": "string", "description": "Commit message."},
                "pr_title": {"type": "string", "description": "Pull request title."},
                "pr_body": {"type": "string", "description": "Pull request description in Markdown."},
            },
            "required": ["repository", "branch", "base", "commit_message", "pr_title", "pr_body"],
        },
    ),
]


def build_github_handlers(session_id: str) -> Dict[str, Handler]:
    def required(args: Dict[str, Any], key: str) -> str:
        value = args.get(key)
        if value in (None, ""):
            raise ValueError(f"Missing required argument '{key}'.")
        return str(value)

    async def checkout(args: Dict[str, Any]) -> Tuple[str, Path]:
        repository = required(args, "repository")
        return repository, await ensure_checkout(session_id, repository)

    async def list_repositories(_: Dict[str, Any]) -> Any:
        connection = get_github_connection(session_id)
        if connection is None:
            raise GitHubConnectionError("GitHub is not connected.")
        return {
            "login": connection.login,
            "repositories": [
                {"full_name": name, "local_folder": connection.checkouts.get(name)}
                for name in sorted(connection.selected)
            ],
        }

    async def use_clone(args: Dict[str, Any]) -> Any:
        path = await set_local_folder(session_id, required(args, "repository"), required(args, "folder"))
        return {"checkout": str(path)}

    async def previous_clone(args: Dict[str, Any]) -> Any:
        path = await find_previous_clone(required(args, "repository"))
        return {"checkout": str(path) if path else None}

    async def branches(args: Dict[str, Any]) -> Any:
        return {"branches": await list_branch_names(session_id, required(args, "repository"))}

    async def pull_requests(args: Dict[str, Any]) -> Any:
        state = str(args.get("state") or "open")
        if state not in ("open", "closed", "all"):
            raise GitHubConnectionError("state must be open, closed or all.")
        return {"pull_requests": await list_pull_requests(session_id, required(args, "repository"), state)}

    async def files(args: Dict[str, Any]) -> Any:
        return await list_repo_files(
            session_id,
            required(args, "repository"),
            str(args.get("directory") or ""),
            str(args.get("branch") or ""),
        )

    async def get_file(args: Dict[str, Any]) -> Any:
        repository = required(args, "repository")
        relative = required(args, "path")
        branch = str(args.get("branch") or "")
        return {"path": relative, "content": await read_repo_file(session_id, repository, relative, branch)}

    async def search(args: Dict[str, Any]) -> Any:
        return await search_repo_code(
            session_id, required(args, "repository"), required(args, "pattern"), str(args.get("branch") or "")
        )

    async def write(args: Dict[str, Any]) -> Any:
        _, path = await checkout(args)
        relative = required(args, "path")
        write_file(path, relative, required(args, "content"))
        return {"written": relative}

    async def history(args: Dict[str, Any]) -> Any:
        return await repo_history(
            session_id, required(args, "repository"), str(args.get("branch") or ""), args.get("limit") or 10
        )

    async def changes(args: Dict[str, Any]) -> Any:
        return {"changes": [{"path": c["path"], "status": c["status"]} for c in await repo_changes(session_id, required(args, "repository"))]}

    async def discard(args: Dict[str, Any]) -> Any:
        _, path = await checkout(args)
        relative = required(args, "path")
        await discard_file(path, relative)
        return {"discarded": relative}

    async def create_branch_handler(args: Dict[str, Any]) -> Any:
        repository = required(args, "repository")
        return {"branch": await create_branch(session_id, repository, required(args, "branch"))}

    async def commit_pr(args: Dict[str, Any]) -> Any:
        repository = required(args, "repository")
        branch = required(args, "branch")
        base = required(args, "base")
        connection = get_github_connection(session_id)
        if connection is None:
            raise GitHubConnectionError("GitHub is not connected.")
        sha = await commit_and_push(
            session_id,
            repository,
            branch=branch,
            base=base,
            message=required(args, "commit_message"),
            author_login=connection.login,
            author_id=connection.user_id,
        )
        pull_request = await open_pull_request(
            session_id,
            repository,
            head=branch,
            base=base,
            title=required(args, "pr_title"),
            body=str(args.get("pr_body") or ""),
        )
        return {"commit": sha, "pull_request": pull_request}

    return {
        "list_repositories": list_repositories,
        "use_local_clone": use_clone,
        "find_previous_clone": previous_clone,
        "list_branches": branches,
        "list_pull_requests": pull_requests,
        "list_repo_files": files,
        "get_repo_file": get_file,
        "search_repo_code": search,
        "get_repo_history": history,
        "write_repo_file": write,
        "get_repo_changes": changes,
        "discard_repo_file": discard,
        "create_repo_branch": create_branch_handler,
        "commit_and_open_pr": commit_pr,
    }
