"""Proxy-and-record mode.

Run the mock server in front of a real upstream. Any request that no explicit
route or resource handles is:

  1. looked up in the fixtures directory - if a saved response exists, replay it;
  2. otherwise, forwarded to the upstream, and the response is saved as a
     fixture so the next identical request replays offline.

Set ``upstream`` to ``None`` for pure replay (offline) mode: unknown requests
that have no fixture return 404 instead of hitting the network.

Fixtures are plain JSON on disk, one file per distinct request, so they are
easy to inspect, edit, commit and share. A request is identified by:

  * the method and path,
  * every query parameter, including repeated ones (``?tag=a&tag=b``), in a
    canonical order,
  * a hash of the request body when there is one. JSON bodies are hashed in
    canonical form (sorted keys, no whitespace), so ``{"a":1,"b":2}`` and
    ``{"b": 2, "a": 1}`` share a fixture while different payloads do not.

Fixtures written by 0.1.x have no body hash. They still load, and they are
used as a fallback for any body sent to the same method + path + query, so
existing fixture directories keep replaying.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlencode

import httpx

# Headers we must not forward or replay verbatim.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authorization", "proxy-authenticate", "te", "trailers",
    "content-encoding", "accept-encoding",
}
_TEXTUAL = ("application/json", "text/", "application/xml", "application/javascript",
            "application/x-www-form-urlencoded", "+json", "+xml")

FIXTURE_VERSION = 2

QueryLike = Union[Dict[str, Any], Sequence[Tuple[str, Any]], None]


def body_hash(body: Optional[bytes]) -> Optional[str]:
    """Stable short hash of a request body, or ``None`` for an empty body.

    JSON is canonicalised first so key order and whitespace do not matter.
    """
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        canonical = bytes(body)
    else:
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(canonical).hexdigest()[:16]


def _query_pairs(query: QueryLike) -> List[Tuple[str, str]]:
    """Flatten a query mapping or list of pairs into sorted string pairs."""
    if not query:
        return []
    items: Iterable[Tuple[str, Any]] = query.items() if isinstance(query, dict) else query
    flat: List[Tuple[str, str]] = []
    for key, value in items:
        if isinstance(value, (list, tuple)):
            flat.extend((str(key), str(v)) for v in value)
        else:
            flat.append((str(key), str(value)))
    return sorted(flat)


def _query_for_fixture(pairs: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Readable query dict: single values stay scalars, repeats become lists."""
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(value)
        else:
            out[key] = value
    return out


class Recorder:
    """Loads existing fixtures and records new ones on cache miss."""

    def __init__(
        self,
        upstream: Optional[str],
        fixtures_dir: str = "fixtures",
        record: bool = True,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.upstream = upstream.rstrip("/") if upstream else None
        self.fixtures_dir = fixtures_dir
        self.record = record and bool(upstream)
        self._transport = transport
        self._store: Dict[str, Dict[str, Any]] = {}
        self._legacy: Dict[str, Dict[str, Any]] = {}
        self._client: Optional[httpx.AsyncClient] = None
        if self.record:
            os.makedirs(self.fixtures_dir, exist_ok=True)
        self._load()

    # -- fixture keys ----------------------------------------------------- #
    @staticmethod
    def _key(method: str, path: str, query: QueryLike, body_sha1: Optional[str] = None) -> str:
        qs = urlencode(_query_pairs(query))
        key = f"{method.upper()} {path}?{qs}"
        if body_sha1:
            key += f" body={body_sha1}"
        return key

    @staticmethod
    def _filename(key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return f"{digest}.json"

    def key_for(self, info: Dict[str, Any]) -> str:
        """The fixture key for an incoming request (see module docstring)."""
        query = info.get("query_items", info.get("query"))
        return self._key(info["method"], info["path"], query, body_hash(info.get("body")))

    @property
    def fixture_count(self) -> int:
        return len(self._store) + len(self._legacy)

    # -- persistence ------------------------------------------------------ #
    def _load(self) -> None:
        if not os.path.isdir(self.fixtures_dir):
            return
        for name in sorted(os.listdir(self.fixtures_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.fixtures_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    fixture = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(fixture, dict) or "response" not in fixture:
                continue
            req = fixture.get("request", {}) or {}
            method, fpath, query = req.get("method", "GET"), req.get("path", "/"), req.get("query", {})
            if "body_sha1" in req:
                self._store[self._key(method, fpath, query, req.get("body_sha1"))] = fixture
            else:
                # 0.1.x fixture: no body hash was recorded.
                self._legacy[self._key(method, fpath, query)] = fixture

    def _save(self, key: str, fixture: Dict[str, Any]) -> str:
        path = os.path.join(self.fixtures_dir, self._filename(key))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, ensure_ascii=False)
        return path

    def lookup(self, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the saved fixture for this request, if any."""
        fixture = self._store.get(self.key_for(info))
        if fixture is not None:
            return fixture
        query = info.get("query_items", info.get("query"))
        return self._legacy.get(self._key(info["method"], info["path"], query))

    # -- upstream client -------------------------------------------------- #
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0, follow_redirects=True, transport=self._transport
            )
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
        if not isinstance(body, str):
            # Hand-edited fixture with a JSON value instead of a string.
            return json.dumps(body, ensure_ascii=False).encode("utf-8")
        return body.encode("utf-8")

    def _clean_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}

    async def handle(self, info: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, str]]:
        """Return (status, body_bytes, headers) for an unmatched request."""
        method = info["method"]
        path = info["path"]

        fixture = self.lookup(info)
        if fixture is not None:
            resp = fixture["response"]
            headers = self._clean_headers(resp.get("headers", {}))
            headers["X-Mock-Source"] = "replay"
            return int(resp.get("status", 200)), self._decode_body(resp), headers

        if not self.upstream:
            body = json.dumps({
                "error": "no_fixture",
                "message": f"No recorded fixture for {method} {path} and no upstream configured.",
                "fixture_key": self.key_for(info),
            }).encode("utf-8")
            return 404, body, {"content-type": "application/json", "X-Mock-Source": "replay-miss"}

        status, resp_body, resp_headers = await self._forward(info)

        if self.record:
            key = self.key_for(info)
            fixture = self._build_fixture(
                method, path, info.get("query_items", info.get("query")), status, resp_body, resp_headers,
                request_body=info.get("body"),
            )
            self._store[key] = fixture
            self._save(key, fixture)

        out_headers = self._clean_headers(resp_headers)
        out_headers["X-Mock-Source"] = "upstream"
        return status, resp_body, out_headers

    async def _forward(self, info: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, str]]:
        client = await self._get_client()
        url = self.upstream + info["path"]
        send_headers = {k: v for k, v in info.get("headers", {}).items() if k.lower() not in _HOP_BY_HOP}
        params = info.get("query_items")
        if params is None:
            params = list((info.get("query") or {}).items())
        response = await client.request(
            info["method"],
            url,
            params=params,
            content=info.get("body") or None,
            headers=send_headers,
        )
        return response.status_code, response.content, dict(response.headers)

    def _build_fixture(
        self,
        method: str,
        path: str,
        query: QueryLike,
        status: int,
        body: bytes,
        headers: Dict[str, str],
        request_body: Optional[bytes] = None,
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
        request: Dict[str, Any] = {
            "method": method.upper(),
            "path": path,
            "query": _query_for_fixture(_query_pairs(query)),
            "body_sha1": body_hash(request_body),
        }
        if request_body:
            # Informational copy so a fixture file explains itself; matching
            # only uses body_sha1.
            try:
                request["body"] = json.loads(request_body)
            except (ValueError, UnicodeDecodeError):
                try:
                    request["body"] = request_body.decode("utf-8")[:4096]
                except UnicodeDecodeError:
                    request["body"] = None
        return {
            "version": FIXTURE_VERSION,
            "request": request,
            "response": {
                "status": status,
                "headers": self._clean_headers(headers),
                "body": encoded,
                "body_encoding": encoding,
            },
        }


def build_recorder(
    record_config: Dict[str, Any],
    base_dir: str = ".",
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Recorder:
    """Construct a Recorder from a mocks-file ``record`` block."""
    fixtures = record_config.get("fixtures_dir") or "fixtures"
    if not os.path.isabs(fixtures):
        fixtures = os.path.join(base_dir, fixtures)
    return Recorder(
        upstream=record_config.get("upstream"),
        fixtures_dir=fixtures,
        record=bool(record_config.get("record", True)),
        transport=transport,
    )
