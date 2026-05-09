from __future__ import annotations

import asyncio
import os
from typing import Any

from ..utils import csv_env, is_internal_issue_artifact, max_chars_from_env, truncate_text
from ._base import GitHubSkillBase
from ._models import (
    DELETED_MEMORY_LABEL,
    MEMORY_LABEL,
    DispatchWorkflowArgs,
    ReadWorkflowRunArgs,
    RunCommandArgs,
    SearchRepoContextArgs,
)
from ._utils import (
    _allowed_command_prefixes,
    _command_timeout_seconds,
    _is_allowed_command,
    _parse_safe_command,
    _safe_subprocess_env,
)


class SearchRepoContext(GitHubSkillBase):
    name = "search_repo_context"
    description = (
        "Search non-memory issues and pull requests across the repository. "
        "By default this excludes archived memory records and internal bot-maintenance artifacts. "
        "Use include_internal=true only when you intentionally need those internal artifacts."
    )
    args_model = SearchRepoContextArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        kind = args.kind.strip().lower()
        if kind not in {"all", "issues", "prs"}:
            return "Invalid kind. Use one of: all, issues, prs."
        context = self._require_context()
        filters = [f"repo:{context['owner']}/{context['repo']}", f"-label:\"{MEMORY_LABEL}\"", f"-label:\"{DELETED_MEMORY_LABEL}\""]
        if kind == "issues":
            filters.append("is:issue")
        elif kind == "prs":
            filters.append("is:pr")
        q = " ".join([*filters, args.query])
        result = await self._api.get_json(
            "/search/issues",
            params={"q": q, "per_page": args.limit, "sort": "updated", "order": "desc"},
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not args.include_internal:
            items = [item for item in items if not is_internal_issue_artifact(item)]
        if not items:
            return f"No repo context results for: {args.query}"
        lines = [f"Repo context results for '{args.query}' ({result.get('total_count', len(items))} total):"]
        for item in items:
            issue_type = "PR" if "pull_request" in item else "Issue"
            labels = ", ".join(self._labels_from_issue(item)) or "none"
            lines.append(
                f"  #{item['number']} [{issue_type}] {item['title']} "
                f"state={item.get('state', '')} labels={labels} "
                f"updated={item.get('updated_at', '')} url={item.get('html_url', '')}"
            )
        return "\n".join(lines)


class DispatchWorkflow(GitHubSkillBase):
    name = "dispatch_workflow"
    description = (
        "Trigger a GitHub Actions workflow by its filename (e.g. 'ci.yml') "
        "or numeric ID. The workflow must have a workflow_dispatch trigger. "
        "Use this to run tests, lint, deploy, or any CI pipeline already "
        "defined in the repository. "
        "If the workflow doesn't exist, GitHub will return a 404 error."
    )
    args_model = DispatchWorkflowArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        allowed_refs = csv_env("RYOBOT_ALLOWED_WORKFLOW_REFS") or {"main"}
        if args.ref not in allowed_refs:
            return f"Workflow ref '{args.ref}' is not allowed for dispatch."
        inputs = args.inputs if isinstance(args.inputs, dict) else {}
        await self._api.post_no_content(
            f"/repos/{context['owner']}/{context['repo']}/actions/workflows/{args.workflow_id}/dispatches",
            json_body={"ref": args.ref, "inputs": inputs},
        )
        return (
            f"Dispatched workflow '{args.workflow_id}' on ref '{args.ref}'. "
            f"Use read_workflow_run with workflow_id='{args.workflow_id}' to check the result."
        )


class ReadWorkflowRun(GitHubSkillBase):
    name = "read_workflow_run"
    description = (
        "Read the status of a GitHub Actions workflow run. "
        "If run_id is provided, reads that specific run. "
        "Otherwise reads the latest run for the given workflow_id. "
        "Returns status, conclusion, duration, and a link to the run."
    )
    args_model = ReadWorkflowRunArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        if args.run_id > 0:
            run = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/actions/runs/{args.run_id}"
            )
        elif args.workflow_id:
            runs = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/actions/workflows/{args.workflow_id}/runs",
                params={"per_page": 1},
            )
            if not runs.get("workflow_runs"):
                return f"No runs found for workflow '{args.workflow_id}'."
            run = runs["workflow_runs"][0]
        else:
            return "Must provide either workflow_id or run_id."

        return "\n".join(
            [
                f"Workflow: {run.get('name') or args.workflow_id}",
                f"Status: {run.get('status')}",
                f"Conclusion: {run.get('conclusion') or 'N/A'}",
                f"Created: {run.get('created_at')}",
                f"URL: {run.get('html_url')}",
            ]
        )


class RunCommand(GitHubSkillBase):
    name = "run_command"
    description = (
        "Execute a shell command in the repository workspace and return stdout/stderr. "
        "Available: pytest, python -m pytest, ruff check, mypy, pyright, python (scripts). "
        "Not available: pip, npm, docker, git push/commit, interactive commands. "
        "Shell metacharacters (| > < && ;) are rejected — use one command per call. "
        "Secrets are stripped from the subprocess environment."
    )
    args_model = RunCommandArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        workspace = os.getenv("GITHUB_WORKSPACE", ".")
        parsed = _parse_safe_command(args.command)
        if isinstance(parsed, str):
            return parsed
        if not _is_allowed_command(parsed):
            return (
                "Command is not allowed. Allowed command prefixes: "
                + ", ".join(sorted(_allowed_command_prefixes()))
            )
        timeout = _command_timeout_seconds()

        try:
            proc = await asyncio.create_subprocess_exec(
                *parsed,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env=_safe_subprocess_env(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"Command timed out after {timeout}s:\n  {args.command}"
        except FileNotFoundError as exc:
            return f"Command failed to start: {exc}"

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        parts: list[str] = [f"Exit code: {exit_code}"]
        if stdout:
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"stderr:\n{stderr.rstrip()}")
        result = "\n\n".join(parts)

        max_chars = max_chars_from_env("RYOBOT_MAX_TOOL_RESULT_CHARS", 20000)
        return truncate_text(result, max_chars)
