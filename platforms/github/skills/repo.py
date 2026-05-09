from __future__ import annotations

import ast
from typing import Any

import httpx

from ..utils import max_chars_from_env, truncate_text
from ._base import GitHubSkillBase
from ._models import (
    CreateBranchArgs,
    DeleteBranchArgs,
    FindFilePathsArgs,
    GetProjectTreeArgs,
    ListFilesArgs,
    ReadFileArgs,
    SearchCodeArgs,
    SearchSymbolArgs,
    WriteFileArgs,
)


def _render_tree(entries: list[dict[str, Any]], *, max_depth: int) -> str:
    children: dict[str, set[str]] = {}
    files: dict[str, set[str]] = {}
    for entry in entries:
        raw_path = str(entry.get("path") or "").strip("/")
        if not raw_path:
            continue
        parts = raw_path.split("/")
        entry_type = str(entry.get("type") or "")
        if entry_type == "tree":
            for depth in range(min(len(parts), max_depth)):
                parent = "/".join(parts[:depth])
                children.setdefault(parent, set()).add(parts[depth])
        elif entry_type == "blob":
            for depth in range(min(len(parts) - 1, max_depth)):
                parent = "/".join(parts[:depth])
                children.setdefault(parent, set()).add(parts[depth])
            if len(parts) <= max_depth:
                parent = "/".join(parts[:-1])
                files.setdefault(parent, set()).add(parts[-1])

    lines = ["Project tree:"]

    def walk(parent: str, depth: int) -> None:
        if depth > max_depth:
            return
        for dirname in sorted(children.get(parent, set())):
            indent = "  " * depth
            lines.append(f"{indent}📁 {dirname}/")
            child_parent = f"{parent}/{dirname}" if parent else dirname
            walk(child_parent, depth + 1)
        for filename in sorted(files.get(parent, set())):
            indent = "  " * depth
            lines.append(f"{indent}📄 {filename}")

    walk("", 0)
    return "\n".join(lines)


class GetProjectTree(GitHubSkillBase):
    name = "get_project_tree"
    description = (
        "Fetch a repo-wide tree snapshot. Use this first when you need a global map of the repository instead of "
        "walking directories one by one."
    )
    args_model = GetProjectTreeArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        effective_ref, payload = await self._load_project_tree(context, args.ref)
        return f"{_render_tree(payload['entries'], max_depth=args.max_depth)}\n(ref: {effective_ref}, max_depth: {args.max_depth})"


class FindFilePaths(GitHubSkillBase):
    name = "find_file_paths"
    description = (
        "Find repository paths by keyword using the cached project tree. Prefer this over code search when you need "
        "candidate files by name or path fragment."
    )
    args_model = FindFilePathsArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        keyword = args.keyword.strip()
        if not keyword:
            return "Keyword is empty."
        effective_ref, payload = await self._load_project_tree(context, args.ref)
        needle = keyword.casefold()
        matches = [
            str(entry.get("path") or "")
            for entry in payload["entries"]
            if needle in str(entry.get("path") or "").casefold()
        ]
        if not matches:
            return f"No file paths found for '{keyword}' (ref: {effective_ref})."
        lines = [f"File path matches for '{keyword}' (ref: {effective_ref}):"]
        lines.extend(f"  {path}" for path in matches[:100])
        if len(matches) > 100:
            lines.append(f"  ... and {len(matches) - 100} more matches")
        return "\n".join(lines)


class SearchSymbol(GitHubSkillBase):
    name = "search_symbol"
    description = (
        "Locate Python symbol definitions using AST parsing. Use this to find classes, functions, methods, async "
        "functions, or module-level variables without noisy regex search."
    )
    args_model = SearchSymbolArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        symbol_name = args.symbol_name.strip()
        if not symbol_name:
            return "symbol_name is empty."
        effective_ref, payload = await self._load_project_tree(context, args.ref)
        matches: list[str] = []
        for entry in payload["entries"]:
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            if not path.endswith(".py"):
                continue
            blob_sha = str(entry.get("sha") or "")
            if not blob_sha:
                continue
            try:
                source = await self._read_blob_text(context, blob_sha)
                tree = ast.parse(source, filename=path)
            except (SyntaxError, UnicodeDecodeError, ValueError):
                continue
            matches.extend(self._symbol_matches(path=path, symbol_name=symbol_name, tree=tree))
        if not matches:
            return f"No Python symbol definitions found for '{symbol_name}' (ref: {effective_ref})."
        lines = [f"Python symbol matches for '{symbol_name}' (ref: {effective_ref}):"]
        lines.extend(f"  {line}" for line in matches[:100])
        if len(matches) > 100:
            lines.append(f"  ... and {len(matches) - 100} more matches")
        return "\n".join(lines)

    @staticmethod
    def _symbol_matches(*, path: str, symbol_name: str, tree: ast.AST) -> list[str]:
        matches: list[str] = []
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if isinstance(node, ast.ClassDef):
                if node.name == symbol_name:
                    matches.append(f"class {symbol_name} -> {path}:{node.lineno}")
                for child in node.body:
                    if isinstance(child, ast.AsyncFunctionDef) and child.name == symbol_name:
                        matches.append(f"async method {symbol_name} -> {path}:{child.lineno}")
                    elif isinstance(child, ast.FunctionDef) and child.name == symbol_name:
                        matches.append(f"method {symbol_name} -> {path}:{child.lineno}")
            elif isinstance(node, ast.AsyncFunctionDef) and node.name == symbol_name:
                matches.append(f"async function {symbol_name} -> {path}:{node.lineno}")
            elif isinstance(node, ast.FunctionDef) and node.name == symbol_name:
                matches.append(f"function {symbol_name} -> {path}:{node.lineno}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol_name:
                        matches.append(f"module variable {symbol_name} -> {path}:{node.lineno}")
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == symbol_name:
                    matches.append(f"module variable {symbol_name} -> {path}:{node.lineno}")
        return matches


class ListFiles(GitHubSkillBase):
    name = "list_files"
    description = (
        "List files and directories at a given path in the repository. "
        "Use this for local directory expansion after you already have a project-level map. Pass an empty string for the root directory. "
        "Optionally provide a ref (branch, tag, or commit SHA) to list files on a non-default branch. "
        "When the current thread is a pull request and ref is omitted, this defaults to the PR head branch. "
        "Returns file/directory names, types, and sizes."
    )
    args_model = ListFilesArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {}
        effective_ref = self._effective_ref(context, args.ref)
        if effective_ref:
            params["ref"] = effective_ref
        contents = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
            params=params if params else None,
        )
        if not isinstance(contents, list):
            return f"Not a directory: {args.path or '/'}"
        if not contents:
            return f"Directory is empty: {args.path or '/'}"

        ref_note = f" (ref: {effective_ref})" if effective_ref else ""
        lines: list[str] = [f"Contents of {args.path or '/'}{ref_note}:"]
        for item in contents[: args.limit]:
            item_type = item.get("type", "?")
            prefix = "📁" if item_type == "dir" else "📄"
            lines.append(f"  {prefix} {item['name']} ({item_type}, {item.get('size', 0)} bytes)")
        if len(contents) > args.limit:
            lines.append(f"  ... and {len(contents) - args.limit} more entries")
        return "\n".join(lines)


class ReadFile(GitHubSkillBase):
    name = "read_file"
    description = (
        "Read the content of a file in the repository. "
        "Provide the full path relative to the repository root. "
        "Optionally provide a ref (branch, tag, or commit SHA) to read from a "
        "non-default branch. When the current thread is a pull request and ref is omitted, "
        "this defaults to the PR head branch. Returns the decoded file content."
    )
    args_model = ReadFileArgs

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {}
        effective_ref = self._effective_ref(context, args.ref)
        if effective_ref:
            params["ref"] = effective_ref
        item = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
            params=params if params else None,
        )
        if not isinstance(item, dict):
            return f"Cannot read: {args.path}"
        if item.get("type") != "file":
            return f"Not a file: {args.path} (type: {item.get('type', 'unknown')})"
        size = item.get("size", 0)
        if size > 500_000:
            return f"File too large to read: {args.path} ({size} bytes, limit 500KB)"

        content_b64 = item.get("content", "")
        if not content_b64:
            return f"File is empty: {args.path}"
        try:
            decoded = base64.b64decode(content_b64).decode("utf-8")
        except Exception:
            return f"File content could not be decoded as UTF-8: {args.path}"
        max_chars = max_chars_from_env("RYOBOT_MAX_FILE_CHARS", 30000)
        lines = decoded.split("\n")
        truncated = truncate_text(decoded, max_chars)
        ref_note = f", ref {effective_ref}" if effective_ref else ""
        if len(decoded) > max_chars:
            header = (
                f"File: {args.path} ({len(lines)} lines, {size} bytes{ref_note}, "
                f"truncated to {max_chars} chars)\n\n"
            )
        else:
            header = f"File: {args.path} ({len(lines)} lines, {size} bytes{ref_note})\n\n"
        return header + truncated


class SearchCode(GitHubSkillBase):
    name = "search_code"
    description = (
        "Search for code in the repository using GitHub's code search. "
        "Use keywords, regex patterns, or function names only as a fallback after get_project_tree/find_file_paths/search_symbol. "
        "Returns matching file paths and code snippets."
    )
    args_model = SearchCodeArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        q = f"{args.query} repo:{context['owner']}/{context['repo']}"
        result = await self._api.get_json(
            "/search/code",
            params={"q": q, "per_page": args.limit},
        )
        items = result.get("items", [])
        if not items:
            return f"No code results found for '{args.query}'."

        total = result.get("total_count", len(items))
        lines: list[str] = [f"Code search results for '{args.query}' ({total} total, showing top {len(items)}):"]
        for item in items:
            repo_info = item.get("repository", {})
            lines.append(
                f"\n  {item['path']} ({repo_info.get('full_name', '')})\n"
                f"    {item.get('html_url', '')}"
            )
        return "\n".join(lines)


class WriteFile(GitHubSkillBase):
    name = "write_file"
    description = (
        "Create or update a file in the repository. "
        "Parameters: path (file path within repo, e.g. 'src/main.py' — NOT a git branch), "
        "content (new file content, plain text), message (commit message), "
        "branch (git branch to commit to, NOT the file path). "
        "If the file already exists it will be updated; if not, it will be created. "
        "Writing directly to the default branch is not allowed — you must create a "
        "feature branch first, write to it, and then open a pull request."
    )
    args_model = WriteFileArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        repo_info = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}",
        )
        default_branch = repo_info.get("default_branch", "main")
        effective_branch = args.branch or default_branch
        if effective_branch == default_branch:
            return (
                f"Refusing to write directly to the default branch '{default_branch}'. "
                "All code changes must go through the PR workflow:\n"
                "1. Use create_branch to create a feature branch\n"
                "2. Use write_file with branch=<your-branch> to commit changes\n"
                "3. Use create_pull_request to open a PR for review\n"
                "You must never commit directly to the default branch."
            )

        content_bytes = args.content.encode("utf-8")
        body: dict[str, Any] = {
            "message": args.message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": args.branch,
        }

        # Try to get existing file sha for updates
        try:
            existing = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
                params={"ref": args.branch},
            )
            if isinstance(existing, dict) and existing.get("sha"):
                body["sha"] = existing["sha"]
        except Exception:
            pass  # File doesn't exist, will be created

        try:
            result = await self._api.put_json(
                f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        action = "Updated" if body.get("sha") else "Created"
        return (
            f"{action} file {args.path}\n"
            f"Commit: {result.get('commit', {}).get('sha', '')}\n"
            f"URL: {result.get('content', {}).get('html_url', '')}"
        )


class CreateBranch(GitHubSkillBase):
    name = "create_branch"
    description = (
        "Create a new branch in the repository. "
        "Provide the new branch name. Optionally specify a base branch to branch from "
        "(defaults to the repository's default branch)."
    )
    args_model = CreateBranchArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        # Get the SHA of the base branch
        base = args.base_branch
        if not base:
            repo_info = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}",
            )
            base = repo_info.get("default_branch", "main")

        ref_info = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/git/refs/heads/{base}",
        )
        base_sha = ref_info.get("object", {}).get("sha", "")
        if not base_sha:
            return f"Could not get SHA for branch '{base}'."

        try:
            result = await self._api.post_json(
                f"/repos/{context['owner']}/{context['repo']}/git/refs",
                json_body={
                    "ref": f"refs/heads/{args.branch}",
                    "sha": base_sha,
                },
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return (
            f"Created branch '{args.branch}' from '{base}' (SHA: {base_sha[:7]})\n"
            f"URL: {result.get('url', '')}"
        )


class DeleteBranch(GitHubSkillBase):
    name = "delete_branch"
    description = (
        "Delete a branch from the repository. "
        "Use this to clean up stale feature/fix branches after their PR has been merged or closed. "
        "You cannot delete the default branch."
    )
    args_model = DeleteBranchArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        try:
            await self._api.delete_json(
                f"/repos/{context['owner']}/{context['repo']}/git/refs/heads/{args.branch}",
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return f"Deleted branch '{args.branch}'"
