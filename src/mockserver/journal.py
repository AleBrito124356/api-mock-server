"""Request journal: what the frontend actually called.

Every request the mock serves (except calls to the admin API itself) is
recorded in a bounded ring buffer so a test can assert on it afterwards:

    GET    /__mock__/requests?route=create-order     what hit that route
    GET    /__mock__/requests?method=POST&path=/todos*
    DELETE /__mock__/requests                         forget everything

Entries are plain dicts, oldest first, with an ever-increasing ``id`` so a
test can ask for "everything after the id I saw last" with ``?since=``.
"""
from __future__ import annotations

import fnmatch
import itertools
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Mapping, Optional

_MAX_TEXT_BODY = 10_000


def _body_for_journal(body: bytes, parsed: Any, json_valid: bool) -> Any:
    if not body:
        return None
    if json_valid:
        return parsed
    text = body.decode("utf-8", errors="replace")
    if len(text) > _MAX_TEXT_BODY:
        return text[:_MAX_TEXT_BODY] + f"... [{len(text) - _MAX_TEXT_BODY} more characters]"
    return text


class Journal:
    """A bounded, filterable log of served requests."""

    def __init__(self, size: int = 500) -> None:
        self.size = max(0, int(size))
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=self.size or None)
        self._ids = itertools.count(1)

    @property
    def enabled(self) -> bool:
        return self.size > 0

    def __len__(self) -> int:
        return len(self._entries)

    def record(
        self,
        info: Mapping[str, Any],
        status: int,
        matched: Optional[Mapping[str, Any]],
        duration_ms: float,
    ) -> None:
        if not self.enabled:
            return
        query: Dict[str, Any] = {}
        for key, value in info.get("query_items") or []:
            if key in query:
                existing = query[key]
                query[key] = (existing if isinstance(existing, list) else [existing]) + [value]
            else:
                query[key] = value
        self._entries.append({
            "id": next(self._ids),
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "method": info["method"],
            "path": info["path"],
            "query": query,
            "headers": dict(info.get("headers") or {}),
            "body": _body_for_journal(info.get("body") or b"", info.get("json"), bool(info.get("json_valid"))),
            "matched": dict(matched) if matched else None,
            "status": status,
            "duration_ms": round(duration_ms, 2),
        })

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        return count

    def query(self, filters: Mapping[str, str]) -> List[Dict[str, Any]]:
        """Entries matching ``method``, ``path`` (glob), ``route``, ``status``,
        ``since`` (id) and ``limit`` (keep the newest N)."""
        entries = list(self._entries)
        method = filters.get("method")
        if method:
            entries = [e for e in entries if e["method"] == method.upper()]
        path = filters.get("path")
        if path:
            if any(ch in path for ch in "*?["):
                entries = [e for e in entries if fnmatch.fnmatchcase(e["path"], path)]
            else:
                entries = [e for e in entries if e["path"] == path]
        route = filters.get("route")
        if route:
            entries = [e for e in entries if (e["matched"] or {}).get("name") == route]
        status = filters.get("status")
        if status:
            entries = [e for e in entries if str(e["status"]) == str(status)]
        since = filters.get("since")
        if since:
            try:
                threshold = int(since)
            except ValueError:
                threshold = 0
            entries = [e for e in entries if e["id"] > threshold]
        limit = filters.get("limit")
        if limit:
            try:
                n = int(limit)
            except ValueError:
                n = 0
            if n > 0:
                entries = entries[-n:]
        return entries
