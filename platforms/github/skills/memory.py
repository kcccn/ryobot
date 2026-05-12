from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from ._base import GitHubSkillBase
from ._models import (
    RefineMemoryArgs,
    RetrieveMemoryArgs,
    StoreMemoryArgs,
)

_MEMORY_DIR = "memory"
_INDEX_PATH = f"{_MEMORY_DIR}/INDEX.md"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_MAX_OPLOG_ENTRIES = 15


class StoreMemory(GitHubSkillBase):
    name = "store_memory"
    description = (
        "Store a durable memory record as a file in the memory/ directory. "
        "Use this for long-lived project patterns, lessons, invariants, and decisions. "
        "Check the memory index first to avoid duplicates; prefer refine over creating new."
    )
    args_model = StoreMemoryArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        owner = context["owner"]
        repo = context["repo"]
        slug = args.slug.strip()
        if not slug:
            return "Error: slug must be non-empty."

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tags_yaml = "[" + ", ".join(args.tags) + "]" if args.tags else "[]"
        frontmatter = (
            f"---\n"
            f"name: {slug}\n"
            f"description: {args.title}\n"
            f"metadata:\n"
            f"  type: {args.type}\n"
            f"  tags: {tags_yaml}\n"
            f"  status: active\n"
            f"  created: {now}\n"
            f"  updated: {now}\n"
            f"---"
        )
        content = f"{frontmatter}\n\n# {args.title}\n\n{args.summary}\n"
        file_path = f"{_MEMORY_DIR}/{slug}.md"

        existing_sha = None
        try:
            existing = await self._api.get_json(
                f"/repos/{owner}/{repo}/contents/{file_path}"
            )
            existing_sha = existing.get("sha")
        except Exception:
            pass

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        put_body: dict[str, Any] = {
            "message": f"store memory: {slug}",
            "content": encoded,
        }
        if existing_sha:
            put_body["sha"] = existing_sha

        await self._api.put_json(
            f"/repos/{owner}/{repo}/contents/{file_path}",
            json_body=put_body,
        )

        await self._upsert_index(owner, repo, slug, args.title)

        return f"Stored memory '{slug}': {args.title}"

    async def _upsert_index(self, owner: str, repo: str, slug: str, title: str) -> None:
        index_line = f"- [{slug}]({slug}.md) — {title}"

        existing_content = ""
        existing_sha = None
        try:
            resp = await self._api.get_json(
                f"/repos/{owner}/{repo}/contents/{_INDEX_PATH}"
            )
            existing_sha = resp.get("sha")
            raw = resp.get("content", "")
            if raw:
                existing_content = base64.b64decode(raw).decode("utf-8")
        except Exception:
            pass

        lines = existing_content.split("\n") if existing_content else []
        new_lines: list[str] = []
        found = False
        slug_pattern = re.compile(rf"^-?\s*\[{re.escape(slug)}\]")
        for line in lines:
            if slug_pattern.match(line.strip()):
                new_lines.append(index_line)
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(index_line)

        new_content = "\n".join(line for line in new_lines if line.strip() or new_lines.index(line) == len(new_lines) - 1).strip()
        # Keep non-empty lines
        clean_lines = [ln for ln in new_lines if ln.strip()]
        new_content = "\n".join(clean_lines) + "\n"
        encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
        put_body: dict[str, Any] = {
            "message": f"update memory index: {slug}",
            "content": encoded,
        }
        if existing_sha:
            put_body["sha"] = existing_sha

        await self._api.put_json(
            f"/repos/{owner}/{repo}/contents/{_INDEX_PATH}",
            json_body=put_body,
        )


class RetrieveMemory(GitHubSkillBase):
    name = "retrieve_memory"
    description = (
        "Search the memory/INDEX.md for relevant records and return matching entries. "
        "Uses keyword matching against slug and title."
    )
    args_model = RetrieveMemoryArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        owner = context["owner"]
        repo = context["repo"]

        index_md = await self._fetch_file_content(owner, repo, _INDEX_PATH)
        if not index_md:
            return "(no memories yet)"

        query_lower = args.query.lower()
        terms = [t for t in query_lower.split() if t]
        scored: list[tuple[int, str, str]] = []
        link_re = re.compile(r"^-?\s*\[([^\]]+)\]\(([^)]+)\)\s*—\s*(.*)")

        for line in index_md.split("\n"):
            stripped = line.strip()
            # Skip strikethrough (archived) lines
            if stripped.startswith("~~"):
                continue
            m = link_re.match(stripped)
            if not m:
                continue
            slug = m.group(1)
            title = m.group(3)
            haystack = f"{slug} {title}".lower()
            score = 0
            if query_lower and query_lower in haystack:
                score += 50
            score += sum(8 for term in terms if term in haystack)
            if score > 0:
                scored.append((score, slug, stripped))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return f"No memory results for: {args.query}"

        results: list[str] = []
        results.append(f"Memory results for '{args.query}':")
        for _score, slug, _line in scored[:args.limit]:
            file_path = f"{_MEMORY_DIR}/{slug}.md"
            content = await self._fetch_file_content(owner, repo, file_path)
            if content:
                body = _strip_frontmatter(content)
                results.append(f"  [{slug}] {body[:500]}")
            else:
                results.append(f"  [{slug}] (file not found)")

        return "\n".join(results) if len(results) > 1 else f"No memory results for: {args.query}"

    async def _fetch_file_content(self, owner: str, repo: str, path: str) -> str:
        try:
            resp = await self._api.get_json(
                f"/repos/{owner}/{repo}/contents/{path}"
            )
            raw = resp.get("content", "")
            if raw:
                return base64.b64decode(raw).decode("utf-8")
        except Exception:
            pass
        return ""


class RefineMemory(GitHubSkillBase):
    name = "refine_memory"
    description = (
        "Update or archive an existing memory record. "
        "Use action='update' to refine content, or action='archive' to mark as deleted."
    )
    args_model = RefineMemoryArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        owner = context["owner"]
        repo = context["repo"]
        slug = args.slug.strip()
        if not slug:
            return "Error: slug must be non-empty."

        file_path = f"{_MEMORY_DIR}/{slug}.md"

        existing = None
        try:
            existing = await self._api.get_json(
                f"/repos/{owner}/{repo}/contents/{file_path}"
            )
        except Exception:
            return f"Error: memory '{slug}' not found at {file_path}"

        existing_sha = existing.get("sha")
        raw = existing.get("content", "")
        existing_content = base64.b64decode(raw).decode("utf-8") if raw else ""

        if args.action == "archive":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            new_content = _update_frontmatter(existing_content, status="deleted", updated=now)
            encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
            await self._api.put_json(
                f"/repos/{owner}/{repo}/contents/{file_path}",
                json_body={
                    "message": f"archive memory: {slug}",
                    "content": encoded,
                    "sha": existing_sha,
                },
            )
            await self._update_index_line(owner, repo, slug, archived=True)
            return f"Archived memory '{slug}'."

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_title = args.title.strip() or None
        new_summary = args.summary.strip() or None
        new_tags = args.tags if args.tags else None

        new_content = _update_frontmatter(
            existing_content,
            title=new_title,
            summary=new_summary,
            tags=new_tags,
            updated=now,
        )

        encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
        await self._api.put_json(
            f"/repos/{owner}/{repo}/contents/{file_path}",
            json_body={
                "message": f"refine memory: {slug}",
                "content": encoded,
                "sha": existing_sha,
            },
        )

        display_title = new_title or _extract_title_from_content(existing_content) or slug
        await self._update_index_line(owner, repo, slug, new_title=display_title, archived=False)

        return f"Refined memory '{slug}'."

    async def _update_index_line(
        self, owner: str, repo: str, slug: str, *,
        new_title: str | None = None,
        archived: bool = False,
    ) -> None:
        try:
            resp = await self._api.get_json(
                f"/repos/{owner}/{repo}/contents/{_INDEX_PATH}"
            )
            existing_sha = resp.get("sha")
            raw = resp.get("content", "")
            existing_content = base64.b64decode(raw).decode("utf-8") if raw else ""
        except Exception:
            return

        lines = existing_content.split("\n")
        slug_pattern = re.compile(rf"^-?\s*\[{re.escape(slug)}\]")
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if slug_pattern.match(stripped):
                if archived:
                    # Remove strikethrough wrapper if already there, then add
                    inner = stripped.removeprefix("~~").removesuffix("~~")
                    new_lines.append(f"~~{inner}~~")
                elif new_title:
                    new_lines.append(f"- [{slug}]({slug}.md) — {new_title}")
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        clean_lines = [ln for ln in new_lines if ln.strip()]
        new_content = "\n".join(clean_lines) + "\n"
        encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
        put_body: dict[str, Any] = {
            "message": f"update memory index: {slug}",
            "content": encoded,
            "sha": existing_sha,
        }
        await self._api.put_json(
            f"/repos/{owner}/{repo}/contents/{_INDEX_PATH}",
            json_body=put_body,
        )


def _strip_frontmatter(content: str) -> str:
    m = _FRONTMATTER_RE.match(content)
    if m:
        return content[m.end():].strip()
    return content.strip()


def _update_frontmatter(
    content: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    updated: str | None = None,
) -> str:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return content

    fm_text = m.group(1)
    body = content[m.end():]

    new_fm_lines: list[str] = []
    for line in fm_text.split("\n"):
        stripped = line.strip()
        if title is not None and stripped.startswith("description:"):
            new_fm_lines.append(f"description: {title}")
        elif updated is not None and stripped.startswith("updated:"):
            indent = line[:len(line) - len(line.lstrip())]
            new_fm_lines.append(f"{indent}updated: {updated}")
        elif status is not None and stripped.startswith("status:"):
            indent = line[:len(line) - len(line.lstrip())]
            new_fm_lines.append(f"{indent}status: {status}")
        elif tags is not None and stripped.startswith("tags:"):
            indent = line[:len(line) - len(line.lstrip())]
            tags_yaml = "[" + ", ".join(tags) + "]"
            new_fm_lines.append(f"{indent}tags: {tags_yaml}")
        else:
            new_fm_lines.append(line)

    new_fm = "\n".join(new_fm_lines)

    if summary is not None:
        # Find the first heading line and keep it, replace the rest
        body_stripped = body.strip()
        heading_end = body_stripped.find("\n")
        if heading_end > 0 and body_stripped.startswith("# "):
            heading = body_stripped[:heading_end]
            return f"---\n{new_fm}\n---\n\n{heading}\n\n{summary}\n"
        return f"---\n{new_fm}\n---\n\n{summary}\n"

    return f"---\n{new_fm}\n---{body}"


def _extract_title_from_content(content: str) -> str:
    body = _strip_frontmatter(content)
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""
