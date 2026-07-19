"""The ASGI application: request matching and the response pipeline.

The whole server is one Starlette catch-all route. Starlette's own router
only dispatches on method + path, but this server also matches on query,
header and body, so it does its own matching inside the catch-all. The
match pipeline is:

    request
      -> explicit routes   (first match wins; ties broken by priority)
      -> stateful resources (CRUD)
      -> record / replay    (if a record block is configured)
      -> index / 404

For each matched explicit route the behavior pipeline runs before the
response is built: rate limit -> error injection -> latency -> template render.
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .behavior import BehaviorEngine
from .config import MockConfig, ResourceSpec, ResponseSpec, RouteSpec
from .dynamic import TemplateEngine
from .record import build_recorder
from .stateful import ResourceRouter

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

_CORS_HEADERS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
    "access-control-allow-headers": "*",
    "access-control-max-age": "600",
}


class Matcher:
    """Explicit routes sorted by priority (desc) then declaration order."""

    def __init__(self, routes: List[RouteSpec]) -> None:
        self.routes = sorted(routes, key=lambda r: (-r.priority, r.order))

    def match(self, method: str, path: str, info: Dict[str, Any]) -> Optional[Tuple[RouteSpec, Dict[str, str]]]:
        effective = "GET" if method == "HEAD" else method
        for route in self.routes:
            if route.method not in (effective, "ANY", "*"):
                continue
            m = route.regex.match(path)
            if not m:
                continue
            if not _conditions_met(route.match, info):
                continue
            return route, m.groupdict()
        return None


def _conditions_met(match: Dict[str, Any], info: Dict[str, Any]) -> bool:
    if not match:
        return True
    query = info.get("query") or {}
    for key, expected in (match.get("query") or {}).items():
        actual = query.get(key)
        if expected == "*":
            if actual is None:
                return False
        elif actual is None or str(actual) != str(expected):
            return False

    headers = info.get("headers") or {}
    for key, expected in (match.get("headers") or {}).items():
        actual = headers.get(key.lower())
        if expected == "*":
            if actual is None:
                return False
        elif actual is None or str(actual) != str(expected):
            return False

    if "body" in match:
        if not _subset(match["body"], info.get("json")):
            return False
    return True


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_subset(e, a) for e, a in zip(expected, actual))
    return expected == actual


class MockServer:
    """Owns the engines and the per-request dispatch logic."""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.engine = TemplateEngine(seed=config.seed)
        self.behavior = BehaviorEngine(seed=config.seed)
        self.matcher = Matcher(config.routes)
        self.resources = ResourceRouter(config.resources)
        self.recorder = build_recorder(config.record, config.base_dir) if config.record else None

    # -- request context -------------------------------------------------- #
    async def _build_info(self, request: Request) -> Dict[str, Any]:
        body = await request.body()
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "json": parsed,
            "body": body,
        }

    def _render_ctx(self, info: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
        return {
            "method": info["method"],
            "query": info["query"],
            "path": params,
            "headers": info["headers"],
            "json": info.get("json"),
            "body": info.get("json"),
        }

    # -- behavior gate ---------------------------------------------------- #
    async def _behavior_gate(
        self,
        key: str,
        latency: Optional[Dict[str, Any]],
        chaos: Optional[Dict[str, Any]],
        ctx: Dict[str, Any],
    ) -> Optional[Response]:
        rl = self.behavior.rate_limited(key, chaos)
        if rl is not None:
            status = int(rl.get("status", 429))
            body = rl.get("body", {"error": "rate_limited", "message": "Too many requests."})
            headers = {"retry-after": str(int(float(rl.get("window_ms", 1000)) / 1000) or 1)}
            return self._make_response(status, self.engine.render(body, ctx), headers)

        errored = self.behavior.should_error(chaos)
        await anyio.sleep(self.behavior.latency_seconds(latency))
        if errored:
            status, body = self.behavior.error_response(chaos)
            return self._make_response(status, self.engine.render(body, ctx), {})
        return None

    # -- explicit routes -------------------------------------------------- #
    async def _serve_route(self, route: RouteSpec, info: Dict[str, Any], params: Dict[str, str]) -> Response:
        ctx = self._render_ctx(info, params)
        latency = route.latency if route.latency is not None else self.config.global_latency
        chaos = route.chaos if route.chaos is not None else self.config.global_chaos
        gate = await self._behavior_gate(f"{route.method}:{route.path}", latency, chaos, ctx)
        if gate is not None:
            return gate

        body = self._resolve_body(route.response, ctx)
        headers = {k: str(self.engine.render(v, ctx)) for k, v in route.response.headers.items()}
        if info["method"] == "HEAD":
            body = None
        return self._make_response(route.response.status, body, headers)

    def _resolve_body(self, response: ResponseSpec, ctx: Dict[str, Any]) -> Any:
        if response.file:
            path = response.file
            if not os.path.isabs(path):
                path = os.path.join(self.config.base_dir, path)
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            if response.file.endswith(".json"):
                return self.engine.render(json.loads(raw), ctx)
            return self.engine.render(raw, ctx)
        if response.body is None:
            return None
        return self.engine.render(response.body, ctx)

    # -- resources -------------------------------------------------------- #
    async def _serve_resource(self, op: Dict[str, Any], info: Dict[str, Any]) -> Response:
        spec: ResourceSpec = op["spec"]
        ctx = self._render_ctx(info, {})
        latency = spec.latency if spec.latency is not None else self.config.global_latency
        chaos = spec.chaos if spec.chaos is not None else self.config.global_chaos
        gate = await self._behavior_gate(f"resource:{spec.name}", latency, chaos, ctx)
        if gate is not None:
            return gate

        store = self.resources.store_for(spec)
        kind = op["kind"]

        if kind == "method_not_allowed":
            return self._make_response(
                405, {"error": "method_not_allowed", "allow": op["allow"]}, {"allow": op["allow"]}
            )
        if kind == "list":
            items, total = store.list(info["query"])
            body = None if info["method"] == "HEAD" else items
            return self._make_response(200, body, {"x-total-count": str(total)})
        if kind == "get":
            item = store.get(op["id"])
            if item is None:
                return self._not_found_item(spec, op["id"])
            return self._make_response(200, None if info["method"] == "HEAD" else item, {})
        if kind == "create":
            item = store.create(info.get("json") or {})
            location = f"{spec.path.rstrip('/')}/{item[spec.id_field]}"
            return self._make_response(201, item, {"location": location})
        if kind == "replace":
            item = store.replace(op["id"], info.get("json") or {})
            if item is None:
                return self._not_found_item(spec, op["id"])
            return self._make_response(200, item, {})
        if kind == "update":
            item = store.update(op["id"], info.get("json") or {})
            if item is None:
                return self._not_found_item(spec, op["id"])
            return self._make_response(200, item, {})
        if kind == "delete":
            ok = store.delete(op["id"])
            if not ok:
                return self._not_found_item(spec, op["id"])
            return self._make_response(204, None, {})
        return self._make_response(500, {"error": "unhandled_resource_operation"}, {})

    def _not_found_item(self, spec: ResourceSpec, item_id: Any) -> Response:
        return self._make_response(
            404,
            {"error": "not_found", "resource": spec.name, "id": item_id},
            {},
        )

    # -- responses -------------------------------------------------------- #
    def _make_response(self, status: int, body: Any, headers: Dict[str, str]) -> Response:
        out = {k.lower(): str(v) for k, v in (headers or {}).items()}
        if self.config.cors:
            for k, v in _CORS_HEADERS.items():
                out.setdefault(k, v)
        if body is None:
            content = b""
        elif isinstance(body, (dict, list)):
            out.setdefault("content-type", "application/json")
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            content = bytes(body)
        else:
            out.setdefault("content-type", "text/plain; charset=utf-8")
            content = str(body).encode("utf-8")
        return Response(content=content, status_code=status, headers=out)

    def _cors_preflight(self) -> Response:
        return self._make_response(204, None, {})

    def _index(self) -> Response:
        routes = [{"method": r.method, "path": r.path, "priority": r.priority} for r in self.config.routes]
        resources = [{"name": s.name, "path": s.path} for s in self.config.resources]
        return self._make_response(
            200,
            {
                "server": "api-mock-server",
                "routes": routes,
                "resources": resources,
                "record": bool(self.recorder),
            },
            {},
        )

    def _not_found(self, info: Dict[str, Any]) -> Response:
        return self._make_response(
            404,
            {
                "error": "no_mock_matched",
                "method": info["method"],
                "path": info["path"],
                "hint": "No explicit route, resource or fixture matched this request.",
                "configured_routes": len(self.config.routes),
                "configured_resources": len(self.config.resources),
            },
            {},
        )

    # -- dispatch --------------------------------------------------------- #
    async def dispatch(self, request: Request) -> Response:
        info = await self._build_info(request)

        hit = self.matcher.match(info["method"], info["path"], info)
        if hit is not None:
            route, params = hit
            return await self._serve_route(route, info, params)

        op = self.resources.match(info["method"], info["path"])
        if op is not None:
            return await self._serve_resource(op, info)

        if info["method"] == "OPTIONS" and self.config.cors:
            return self._cors_preflight()

        if self.recorder is not None:
            status, body, headers = await self.recorder.handle(info)
            return self._make_response(status, body, headers)

        if info["method"] in ("GET", "HEAD") and info["path"] in ("/", "/__mock__"):
            return self._index()

        return self._not_found(info)


def create_app(config: MockConfig) -> Starlette:
    """Build a Starlette ASGI app that serves the given mock configuration."""
    server = MockServer(config)

    async def catch_all(request: Request) -> Response:
        return await server.dispatch(request)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if server.recorder is not None:
                await server.recorder.aclose()

    app = Starlette(
        routes=[Route("/{path:path}", catch_all, methods=_ALL_METHODS)],
        lifespan=lifespan,
    )
    app.state.mock_server = server
    return app
