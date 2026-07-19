"""Proxy-and-record mode.

Run the mock server in front of a real upstream. Any request that no explicit
route or resource handles is:

  1. looked up in the fixtures directory - if a saved response exists, replay it;
  2. otherwise, forwarded to the upstream, and the response is saved as a
     fixture so the next identical request replays offline.

Set ``upstream`` to ``None`` for pure replay (offline) mode: unknown requests
that have no fixture return 404 instead of hitting the network.

Fixtures are plain JSON on disk, one file per request, so they are easy to
inspect, edit, commit and share.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

# Headers we must not forward or replay verbatim.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authorization", "proxy-authenticate", "te", "trailers",
    "content-encoding", "accept-encoding",
}
_TEXTUAL = ("application/json", "text/", "application/xml", "application/javascript",
            "application/x-www-form-urlencoded")


class Recorder:
    """Loads existing fixtures and records new ones on cache miss."""

    def __init__(
        self,
        upstream: Optional[str],
        fixtures_dir: str = "fixtures",
        record: bool = True,
    ) -> None:
        self.upstream = upstream.rstrip("/") if upstream else None
        self.fixtures_dir = fixtures_dir
        self.record = record and bool(upstream)
        self._store: Dict[str, Dict[str, Any]] = {}
        self._client: Optional[httpx.AsyncClient] = None
        os.makedirs(self.fixtures_dir, exist_ok=True)
        self._load()

    # -- fixture keys ----------------------------------------------------- #
    @staticmethod
    def _key(method: str, path: str, query: Dict[str, Any]) -> str:
        qs = urlencode(sorted((str(k), str(v)) for k, v in (query or {}).items()))
        return f"{method.upper()} {path}?{qs}"

    @staticmethod
    def _filename(key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return f"{digest}.json"

    # -- persistence ------------------------------------------------------ #
    def _load(self) -> None:
        if not os.path.isdir(self.fixtures_dir):
            return
        for name in os.listdir(self.fixtures_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.fixtures_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    fixture = json.load(fh)
            except (OSError, ValueError):
                continue
            req = fixture.get("request", {})
            key = self._key(req.get("method", "GET"), req.get("path", "/"), req.get("query", {}))
            self._store[key] = fixture

    def _save(self, key: str, fixture: Dict[str, Any]) -> None:
        path = os.path.join(self.fixtures_dir, self._filename(key))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, ensure_ascii=False)

    # -- upstream client -------------------------------------------------- #
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- request handling ------------------------------------------------- #
    @staticmethod
    def _is_textual(content_type: str) -> bool:
        ct = (content_type or "").lower()
        return any(ct.startswith(prefix) or prefix in ct for prefix in _TEXTUAL)

    def _decode_body(self, fixture_response: Dict[str, Any]) -> bytes:
        body = fixture_response.get("body", "")
        if fixture_response.get("body_encoding") == "base64":
            return base64.b64decode(body)
        return body.encode("utf-8")

    def _clean_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}

    async def handle(self, info: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, str]]:
        """Return (status, body_bytes, headers) for an unmatched request."""
        method = info["method"]
        path = info["path"]
        query = info.get("query", {})
        key = self._key(method, path, query)

        fixture = self._store.get(key)
        if fixture is not None:
            resp = fixture["response"]
            headers = self._clean_headers(resp.get("headers", {}))
            headers["X-Mock-Source"] = "replay"
            return int(resp.get("status", 200)), self._decode_body(resp), headers

        if not self.upstream:
            body = json.dumps({
                "error": "no_fixture",
                "message": f"No recorded fixture for {method} {path} and no upstream configured.",
            }).encode("utf-8")
            return 404, body, {"content-type": "application/json", "X-Mock-Source": "replay-miss"}

        status, resp_body, resp_headers = await self._forward(info)

        if self.record:
            fixture = self._build_fixture(method, path, query, status, resp_body, resp_headers)
            self._store[key] = fixture
            self._save(key, fixture)

        out_headers = self._clean_headers(resp_headers)
        out_headers["X-Mock-Source"] = "upstream"
        return status, resp_body, out_headers

    async def _forward(self, info: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, str]]:
        client = await self._get_client()
        url = self.upstream + info["path"]
        send_headers = {k: v for k, v in info.get("headers", {}).items() if k.lower() not in _HOP_BY_HOP}
        response = await client.request(
            info["method"],
            url,
            params=info.get("query", {}),
            content=info.get("body") or None,
            headers=send_headers,
        )
        return response.status_code, response.content, dict(response.headers)

    def _build_fixture(
        self,
        method: str,
        path: str,
        query: Dict[str, Any],
        status: int,
        body: bytes,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        content_type = ""
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v
                break
        if self._is_textual(content_type):
            try:
                encoded, encoding = body.decode("utf-8"), "text"
            except UnicodeDecodeError:
                encoded, encoding = base64.b64encode(body).decode("ascii"), "base64"
        else:
            encoded, encoding = base64.b64encode(body).decode("ascii"), "base64"
        return {
            "request": {"method": method, "path": path, "query": dict(query)},
            "response": {
                "status": status,
                "headers": self._clean_headers(headers),
                "body": encoded,
                "body_encoding": encoding,
            },
        }


def build_recorder(record_config: Dict[str, Any], base_dir: str = ".") -> Recorder:
    """Construct a Recorder from a mocks-file ``record`` block."""
    fixtures = record_config.get("fixtures_dir", "fixtures")
    if not os.path.isabs(fixtures):
        fixtures = os.path.join(base_dir, fixtures)
    return Recorder(
        upstream=record_config.get("upstream"),
        fixtures_dir=fixtures,
        record=bool(record_config.get("record", True)),
    )
