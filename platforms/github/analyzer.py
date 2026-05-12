from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .utils import is_internal_issue_artifact

_THREAD_TITLE_SUFFIXES = (
    "长期追踪",
    "任务看板",
    "项目路线图",
    "路线图",
    "roadmap",
    "tracker",
)


def _iso_at_or_after(value: str, threshold: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed >= threshold


def _normalized_thread_title(title: str) -> str:
    normalized = re.sub(r"[\U00010000-\U0010ffff]", " ", title)
    normalized = re.sub(r"[^\w一-鿿\s]", " ", normalized, flags=re.UNICODE)
    normalized = normalized.lower()
    for suffix in _THREAD_TITLE_SUFFIXES:
        normalized = normalized.replace(suffix.lower(), " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _title_token_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in left.split(" ") if token}
    right_tokens = {token for token in right.split(" ") if token}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def _find_overlapping_issue_pairs(issues: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, left in enumerate(issues):
        left_author = str((left.get("user") or {}).get("login") or "")
        left_labels = {str(label.get("name") or "").lower() for label in left.get("labels", [])}
        left_title = _normalized_thread_title(str(left.get("title") or ""))
        left_created = _parse_iso_datetime(str(left.get("created_at") or ""))
        if not left_author or not left_title or left_created is None:
            continue
        for right in issues[index + 1 :]:
            right_author = str((right.get("user") or {}).get("login") or "")
            if left_author != right_author:
                continue
            right_labels = {str(label.get("name") or "").lower() for label in right.get("labels", [])}
            if left_labels != right_labels:
                continue
            right_title = _normalized_thread_title(str(right.get("title") or ""))
            right_created = _parse_iso_datetime(str(right.get("created_at") or ""))
            if not right_title or right_created is None:
                continue
            if abs((left_created - right_created).total_seconds()) > 7200:
                continue
            if left_title in right_title or right_title in left_title or _title_token_overlap(left_title, right_title) >= 0.7:
                pairs.append((left, right))
    return pairs


def _issue_labels(issue: dict[str, Any]) -> str:
    names = [str(label.get("name") or "") for label in issue.get("labels", [])]
    names = [name for name in names if name]
    return ", ".join(names) if names else "none"


def build_patrol_brief_summary(
    *,
    since: datetime,
    issues: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    recent_closed: list[dict[str, Any]],
) -> str:
    merged_recent = [
        pr for pr in recent_closed
        if pr.get("merged_at") and _iso_at_or_after(str(pr.get("merged_at")), since)
    ]
    external_issues = [
        item for item in issues
        if "pull_request" not in item and not is_internal_issue_artifact(item)
    ]
    fresh_issues = [
        item for item in external_issues
        if _iso_at_or_after(str(item.get("updated_at") or ""), since)
    ]
    stale_issues = [
        item for item in external_issues
        if not _iso_at_or_after(str(item.get("updated_at") or ""), since)
    ]
    stale_prs = [
        pr for pr in pulls
        if not _iso_at_or_after(str(pr.get("updated_at") or ""), since)
    ]
    overlapping_issue_pairs = _find_overlapping_issue_pairs(external_issues)

    lines: list[str] = ["Street-lurker opportunity radar:"]
    lines.append("1. Recent activity in the last 24 hours:")
    lines.append(f"- Fresh open issues: {len(fresh_issues)}")
    lines.append(f"- Open PRs: {len(pulls)}")
    lines.append(f"- Recently merged PRs: {len(merged_recent)}")
    if merged_recent:
        for pr in merged_recent[:3]:
            lines.append(
                f"  - PR #{pr['number']}: {pr['title']} merged_at={pr.get('merged_at', '')}"
            )
    lines.append("2. Stale threads worth reconsidering:")
    if stale_issues:
        for item in stale_issues[:5]:
            lines.append(
                f"- Issue #{item['number']}: {item['title']} labels={_issue_labels(item)} updated={item.get('updated_at', '')}"
            )
    else:
        lines.append("- No stale non-internal open issues.")
    if stale_prs:
        for pr in stale_prs[:5]:
            lines.append(
                f"- PR #{pr['number']}: {pr['title']} updated={pr.get('updated_at', '')}"
            )
    else:
        lines.append("- No stale open PRs.")
    lines.append("3. Ranked open issues (you decide which are actionable):")
    if external_issues:
        def _sort_key(item: dict[str, Any]) -> tuple[int, float]:
            labels = {str(lb.get("name") or "").lower() for lb in item.get("labels", [])}
            if "p0" in labels:
                priority = 0
            elif "p1" in labels:
                priority = 1
            elif "p2" in labels:
                priority = 2
            else:
                priority = 3
            dt = _parse_iso_datetime(str(item.get("updated_at") or ""))
            epoch = dt.timestamp() if dt else 0.0
            return (priority, -epoch)  # newer first within same priority

        ranked = sorted(external_issues, key=_sort_key)
        for item in ranked[:10]:
            labels = {str(lb.get("name") or "").lower() for lb in item.get("labels", [])}
            priority = "P0" if "p0" in labels else "P1" if "p1" in labels else "P2" if "p2" in labels else "—"
            lines.append(
                f"  [{priority}] Issue #{item['number']}: {item['title']} labels={_issue_labels(item)} updated={item.get('updated_at', '')}"
            )
    else:
        lines.append("- No open external issues.")
    lines.append("4. Potential overlapping threads:")
    if overlapping_issue_pairs:
        for left, right in overlapping_issue_pairs[:5]:
            lines.append(f"- Issue #{left['number']} <-> Issue #{right['number']}")
    else:
        lines.append("- No obvious overlapping non-internal open issue pairs.")
    lines.append("5. Heuristic follow-ups when recent activity is quiet:")
    lines.append("- Check whether old trackers/RFCs should be closed, clarified, split, or advanced.")
    lines.append("- Look for doc/test drift, obvious TODO/stub follow-ups, or prerequisites that seem complete but unconnected.")
    lines.append("- If multiple open threads appear to overlap, verify the relationship before deciding to keep, close, cross-link, or ignore them.")
    lines.append("- 'No new issues/PRs in 24h' is not sufficient to do nothing.")
    return "\n".join(lines)
