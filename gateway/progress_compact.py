"""Pure helpers for the Telegram compact live-activity progress card."""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

DEFAULT_TASK_LABEL_MAX_LEN = 80
DEFAULT_LOG_MAX_CHARS = 2500
DEFAULT_TASK_LABEL = "Working"
DEFAULT_LATEST_ACTION = "Starting..."


def sanitize_task_label(text: str, max_len: int = DEFAULT_TASK_LABEL_MAX_LEN) -> str:
    if not text:
        return DEFAULT_TASK_LABEL
    collapsed = re.sub(r"\s+", " ", text).strip()
    if not collapsed:
        return DEFAULT_TASK_LABEL
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def truncate_log_lines(lines: Sequence[str], max_chars: int = DEFAULT_LOG_MAX_CHARS) -> Tuple[List[str], int]:
    if not lines:
        return [], 0
    kept: List[str] = []
    total = 0
    for line in reversed(lines):
        added = len(line) + 1
        if kept and total + added > max_chars:
            break
        kept.append(line)
        total += added
    kept.reverse()
    return kept, len(lines) - len(kept)


def build_expandable_quote(lines: Sequence[str]) -> str:
    if not lines:
        return ""
    body = list(lines)
    if len(body) == 1:
        return f"**> {body[0]}||"
    out = [f"**> {body[0]}"]
    out.extend(f"> {line}" for line in body[1:-1])
    out.append(f"> {body[-1]}||")
    return "\n".join(out)


def render_compact_card(task_label: str, latest_action: str, action_count: int, log_lines: Sequence[str], max_log_chars: int = DEFAULT_LOG_MAX_CHARS) -> str:
    label = sanitize_task_label(task_label)
    action = latest_action.strip() if latest_action else DEFAULT_LATEST_ACTION
    plural = "" if action_count == 1 else "s"
    header = "\n".join([f"📋 {label}", action, f"_{action_count} action{plural}_"])
    kept, omitted = truncate_log_lines(list(log_lines), max_log_chars)
    quote_lines = list(kept)
    if omitted:
        quote_lines.insert(0, f"… {omitted} earlier action{'' if omitted == 1 else 's'} omitted")
    quote = build_expandable_quote(quote_lines)
    return f"{header}\n\n{quote}" if quote else header
