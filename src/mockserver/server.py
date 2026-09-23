"""The ASGI application: request matching and the response pipeline.

The whole server is one Starlette catch-all route. Starlette's own router
only dispatches on method + path, but this server also matches on query,
header and body, so it does its own matching inside the catch-all. The
match pipeline is:

    request
      -> mock index         (GET /__mock__)
      -> CORS preflight     (OPTIONS + Origin + Access-Control-Request-Method)
      -> explicit routes    (first match wins; ties broken by priority)
      -> stateful resources (CRUD)
      -> record / replay    (if a record block is configured)
      -> index / 404

For each matched explicit route the behavior pipeline runs before the
response is built: rate limit -> error injection -> latency -> template render.

Anything that goes wrong while building a response (a broken template, a
missing body file, an unreachable upstream) comes back as a JSON error that
names the route or expression involved, never as an opaque "Internal Server
Error".
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .behavior import BehaviorEngine
from .config import MockConfig, ResourceSpec, ResponseSpec, RouteSpec
from .dynamic import TemplateEngine, TemplateError
from .record import build_recorder
from .stateful import ResourceConflict, ResourceRouter

logger = logging.getLogger("mockserver")

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD"

# Response headers a browser exposes to JS without being told to.
_CORS_SAFELISTED = {
    "cache-control", "content-language", "content-length", "content-type",
    "expires", "last-modified", "pragma",
}

INDEX_PATH = "/__mock__"


class _JsonNull:
    """Marker for "send the JSON literal null" (as opposed to an empty body)."""


JSON_NULL = _JsonNull()


class Matcher:
    """Explicit routes sorted by priority (desc) then declaration order."""

    def __init__(self, routes: List[RouteSpec]) -> None:
        self.routes = sorted(routes, key=lambda r: (-r.priority, r.order))

    def match(
        self,
        method: str,
        path: str,
        info: Dict[str, Any],
        explicit_only: bool = False,
    ) -> Optional[Tuple[RouteSpec, Dict[str, str]]]:
        """First route that matches. ``explicit_only`` ignores ANY/* routes."""
        effective = "GET" if method == "HEAD" else method
        allowed = (effective,) if explicit_only else (effective, "ANY", "*")
        for route in self.routes:
            if route.method not in allowed:
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
        elif actual is None or str(actual) != _match_str(expected):
            return False

    headers = info.get("headers") or {}
    for key, expected in (match.get("headers") or {}).items():
        actual = headers.get(key.lower())
        if expected == "*":
            if actual is None:
                return False
        elif actual is None or str(actual) != _match_str(expected):
            return False

    if "body" in match:
        if not _subset(match["body"], info.get("json")):
            return False
    return True


def _match_str(expected: Any) -> str:
    # YAML turns `flag: true` into a bool; the query string says "true".
    if isinstance(expected, bool):
        return "true" if expected else "false"
    return str(expected)


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


def _is_json_type(content_type: Optional[str]) -> bool:
    ct = (content_type or "").lower()
    return "json" in ct


def _header_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


class MockServer:
    """Owns the engines and the per-request dispatch logic."""

    def __init__(
        self,
        config: MockConfig,
        *,
        upstream_transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self.engine = TemplateEngine(seed=config.seed)
        self.behavior = BehaviorEngine(seed=config.seed)
        self.matcher = Matcher(config.routes)
        self.resources = ResourceRouter(config.resources)
        self._upstream_transport = upstream_transport
        self.recorder = (
            build_recorder(config.record, config.base_dir, transport=upstream_transport)
            if config.record else None
        )

    # -- request context -------------------------------------------------- #
    async def _build_info(self, request: Request) -> Dict[str, Any]:
        body = await request.body()
        parsed: Any = None
        json_valid = False
        if body:
            try:
                parsed = json.loads(body)
                json_valid = True
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "query_items": list(request.query_params.multi_items()),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "json": parsed,
            "json_valid": json_valid,
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
        gate = await self._behavior_gate(route.key, latency, chaos, ctx)
        if gate is not None:
            return self._tag_route(gate, route)

        response = route.response
        headers = {k: _header_str(self.engine.render(v, ctx)) for k, v in response.headers.items()}
        status = self._resolve_status(response, ctx)
        body = self._resolve_body(response, ctx, headers)
        if info["method"] == "HEAD":
            body = None
        return self._tag_route(self._make_response(status, body, headers), route)

    @staticmethod
    def _tag_route(response: Response, route: RouteSpec) -> Response:
        if route.name:
            response.headers["x-matched-route"] = route.name
        return response

    def _resolve_status(self, response: ResponseSpec, ctx: Dict[str, Any]) -> int:
        raw = response.status
        if isinstance(raw, str):
            raw = self.engine.render(raw, ctx)
        try:
            status = int(raw)
        except (TypeError, ValueError):
            raise TemplateError(f"status rendered to {raw!r}, not an integer") from None
        if not 100 <= status <= 599:
            raise TemplateError(f"status {status} is outside 100-599")
        return status

    def _resolve_body(self, response: ResponseSpec, ctx: Dict[str, Any], headers: Dict[str, str]) -> Any:
        if response.file:
            path = response.file
            if not os.path.isabs(path):
                path = os.path.join(self.config.base_dir, path)
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            if response.file.endswith(".json"):
                rendered = self.engine.render(json.loads(raw), ctx)
                return JSON_NULL if rendered is None else rendered
            # Non-JSON files are sent as text, exactly as rendered.
            return self.engine.render(raw, ctx)
        if response.body is None:
            return None
        rendered = self.engine.render(response.body, ctx)
        if rendered is None:
            return JSON_NULL
        if isinstance(rendered, str):
            declared = next((v for k, v in headers.items() if k.lower() == "content-type"), None)
            if _is_json_type(declared):
                return self._json_text(rendered)
        return rendered

    @staticmethod
    def _json_text(text: str) -> Any:
        """A string body declared as JSON: pass JSON documents through, encode the rest."""
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                json.loads(stripped)
                return text.encode("utf-8")
            except ValueError:
                pass
        return _JsonScalar(text)

    # -- resources -------------------------------------------------------- #
    def _object_body(self, spec: ResourceSpec, info: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Response]]:
        """The JSON object sent to a resource, or a 400 explaining why not."""
        if not info.get("body"):
            return {}, None
        if not info.get("json_valid"):
            return None, self._make_response(
                400,
                {"error": "invalid_json", "resource": spec.name,
                 "message": "The request body is not valid JSON."},
                {},
            )
        payload = info.get("json")
        if not isinstance(payload, dict):
            kind = "null" if payload is None else type(payload).__name__
            kind = {"list": "array", "str": "string", "int": "number", "float": "number", "bool": "boolean"}.get(kind, kind)
            return None, self._make_response(
                400,
                {"error": "invalid_body", "resource": spec.name,
                 "message": f"Expected a JSON object, got {kind}."},
                {},
            )
        return payload, None

    async def _serve_resource(self, op: Dict[str, Any], info: Dict[str, Any]) -> Response:
        spec: ResourceSpec = op["spec"]
        kind = op["kind"]

        if kind == "options":
            return self._make_response(204, None, {"allow": op["allow"]})
        if kind == "method_not_allowed":
            return self._make_response(
                405, {"error": "method_not_allowed", "allow": op["allow"]}, {"allow": op["allow"]}
            )

        ctx = self._render_ctx(info, {})
        latency = spec.latency if spec.latency is not None else self.config.global_latency
        chaos = spec.chaos if spec.chaos is not None else self.config.global_chaos
        gate = await self._behavior_gate(f"resource:{spec.name}", latency, chaos, ctx)
        if gate is not None:
            return gate

        store = self.resources.store_for(spec)

        if kind == "list":
            items, total = store.list(info["query"])
            body = None if info["method"] == "HEAD" else items
            return self._make_response(200, body, {"x-total-count": str(total)})
        if kind == "get":
            item = store.get(op["id"])
            if item is None:
                return self._not_found_item(spec, op["id"])
            return self._make_response(200, None if info["method"] == "HEAD" else item, {})

        payload: Dict[str, Any] = {}
        if kind in ("create", "replace", "update"):
            parsed, error = self._object_body(spec, info)
            if error is not None:
                return error
            payload = parsed or {}

        if kind == "create":
            try:
                item = store.create(payload)
            except ResourceConflict as exc:
                return self._make_response(
                    409,
                    {"error": "conflict", "resource": spec.name, "id": exc.item_id,
                     "message": f"{spec.name} {exc.item_id!r} already exists; use PUT or PATCH to change it."},
                    {},
                )
            location = f"{spec.path.rstrip('/')}/{item[spec.id_field]}"
            return self._make_response(201, item, {"location": location})
        if kind == "replace":
            item = store.replace(op["id"], payload)
            if item is None:
                return self._not_found_item(spec, op["id"])
            return self._make_response(200, item, {})
        if kind == "update":
            item = store.update(op["id"], payload)
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
        out = {k.lower(): _header_str(v) for k, v in (headers or {}).items()}
        if body is None or status in (204, 304):
            content = b""
        elif isinstance(body, (bytes, bytearray)):
            content = bytes(body)
        elif isinstance(body, _JsonScalar):
            out.setdefault("content-type", "application/json")
            content = json.dumps(body.value, ensure_ascii=False).encode("utf-8")
        elif body is JSON_NULL:
            out.setdefault("content-type", "application/json")
            content = b"null"
        elif isinstance(body, str):
            out.setdefault("content-type", "text/plain; charset=utf-8")
            content = body.encode("utf-8")
        else:
            # dict, list, bool, int, float: all JSON.
            out.setdefault("content-type", "application/json")
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return Response(content=content, status_code=status, headers=out)

    def _apply_cors(self, response: Response, info: Dict[str, Any]) -> Response:
        """Add CORS headers that work for credentialed and plain requests."""
        origin = info["headers"].get("origin")
        headers = response.headers
        if origin:
            headers.setdefault("access-control-allow-origin", origin)
            headers.setdefault("access-control-allow-credentials", "true")
            vary = headers.get("vary")
            if not vary:
                headers["vary"] = "Origin"
            elif "origin" not in vary.lower():
                headers["vary"] = vary + ", Origin"
        else:
            headers.setdefault("access-control-allow-origin", "*")
        exposed = sorted(
            k for k in headers.keys()
            if k not in _CORS_SAFELISTED and not k.startswith("access-control-") and k != "vary"
        )
        if exposed:
            headers.setdefault("access-control-expose-headers", ", ".join(exposed))
        return response

    def _cors_preflight(self, info: Dict[str, Any]) -> Response:
        requested_method = info["headers"].get("access-control-request-method", "")
        methods = _ALLOW_METHODS
        if requested_method and requested_method.upper() not in methods:
            methods = f"{methods}, {requested_method.upper()}"
        headers = {
            "access-control-allow-methods": methods,
            "access-control-allow-headers": info["headers"].get("access-control-request-headers") or "*",
            "access-control-max-age": "600",
            "vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
        }
        return self._make_response(204, None, headers)

    @staticmethod
    def _is_preflight(info: Dict[str, Any]) -> bool:
        headers = info["headers"]
        return (
            info["method"] == "OPTIONS"
            and "origin" in headers
            and "access-control-request-method" in headers
        )

    def _index(self) -> Response:
        routes = [
            {"name": r.name, "method": r.method, "path": r.path, "priority": r.priority}
            for r in self.config.routes
        ]
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
                "hint": "No explicit route, resource or fixture matched this request. "
                        f"GET {INDEX_PATH} lists what is configured.",
                "configured_routes": len(self.config.routes),
                "configured_resources": len(self.config.resources),
            },
            {},
        )

    # -- dispatch --------------------------------------------------------- #
    async def _route_request(self, info: Dict[str, Any], trace: Dict[str, Any]) -> Response:
        method, path = info["method"], info["path"]

        if method in ("GET", "HEAD") and path == INDEX_PATH:
            trace["matched"] = {"type": "index"}
            return self._index()

        if self.config.cors and self._is_preflight(info):
            # A route declared with `method: OPTIONS` still wins over the
            # automatic preflight answer; ANY/* routes do not.
            hit = self.matcher.match(method, path, info, explicit_only=True)
            if hit is not None:
                route, params = hit
                trace["matched"] = {"type": "route", "name": route.label}
                return await self._serve_route(route, info, params)
            trace["matched"] = {"type": "preflight"}
            return self._cors_preflight(info)

        hit = self.matcher.match(method, path, info)
        if hit is not None:
            route, params = hit
            trace["matched"] = {"type": "route", "name": route.label}
            return await self._serve_route(route, info, params)

        op = self.resources.match(method, path)
        if op is not None:
            trace["matched"] = {"type": "resource", "name": op["spec"].name, "operation": op["kind"]}
            return await self._serve_resource(op, info)

        if method == "OPTIONS" and self.config.cors:
            trace["matched"] = {"type": "preflight"}
            return self._cors_preflight(info)

        if self.recorder is not None:
            trace["matched"] = {"type": "fixture", "name": self.recorder.key_for(info)}
            try:
                status, body, headers = await self.recorder.handle(info)
            except httpx.HTTPError as exc:
                return self._make_response(
                    502,
                    {"error": "upstream_error", "upstream": self.recorder.upstream,
                     "detail": f"{type(exc).__name__}: {exc}"},
                    {"x-mock-source": "upstream-error"},
                )
            return self._make_response(status, body, headers)

        if method in ("GET", "HEAD") and path == "/":
            trace["matched"] = {"type": "index"}
            return self._index()

        trace["matched"] = None
        return self._not_found(info)

    def _error_response(self, exc: Exception, trace: Dict[str, Any], info: Dict[str, Any]) -> Response:
        matched = trace.get("matched") or {}
        label = matched.get("name") or f"{info['method']} {info['path']}"
        body: Dict[str, Any] = {
            "error": "mock_error",
            "route": label,
            "detail": str(exc) or type(exc).__name__,
            "type": type(exc).__name__,
        }
        if isinstance(exc, TemplateError) and exc.expression:
            body["expression"] = exc.expression
        logger.error("mock_error while serving %s: %s", label, body["detail"])
        return self._make_response(500, body, {})

    async def dispatch(self, request: Request) -> Response:
        info = await self._build_info(request)
        trace: Dict[str, Any] = {}
        try:
            response = await self._route_request(info, trace)
        except Exception as exc:  # noqa: BLE001 - a mock must explain its own failures
            response = self._error_response(exc, trace, info)
        if self.config.cors:
            self._apply_cors(response, info)
        return response


class _JsonScalar:
    """A string that must be sent JSON-encoded (``"text"``), not as text/plain."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


def create_app(
    config: MockConfig,
    *,
    upstream_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Starlette:
    """Build a Starlette ASGI app that serves the given mock configuration.

    ``upstream_transport`` lets tests (or embedders) plug an httpx transport
    such as ``httpx.MockTransport`` into record mode instead of the network.
    """
    server = MockServer(config, upstream_transport=upstream_transport)

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
