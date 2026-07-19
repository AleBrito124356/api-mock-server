"""Configuration model and loader.

A mocks file is plain YAML with four optional top-level keys:

    config:     global settings (seed, cors, latency, chaos)
    routes:     explicit endpoint definitions matched first
    resources:  in-memory CRUD collections (see stateful.py)
    record:     proxy-and-record settings (see record.py)

Everything here is data-only: parsing YAML into typed dataclasses and
pre-compiling path patterns to regexes. No I/O beyond reading the file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Tuple

import yaml

# {param} style path segments -> named regex groups.
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def compile_path(path: str) -> Tuple[Pattern[str], List[str]]:
    """Turn ``/products/{id}`` into an anchored regex plus the param names."""
    names: List[str] = []

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        names.append(name)
        return f"(?P<{name}>[^/]+)"

    # Escape literal regex metacharacters in the static parts, then inject groups.
    escaped = re.sub(_PARAM, lambda m: "\x00%s\x00" % m.group(1), path)
    escaped = re.escape(escaped)
    escaped = escaped.replace("\\\x00", "\x00").replace("\x00\\", "\x00")
    pattern = re.sub(r"\x00([A-Za-z_][A-Za-z0-9_]*)\x00", lambda m: "(?P<%s>[^/]+)" % m.group(1), escaped)
    for m in _PARAM.finditer(path):
        names.append(m.group(1))
    # Deduplicate while preserving order (the escaped pass already found them).
    seen: List[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return re.compile("^" + pattern + "$"), seen


@dataclass
class ResponseSpec:
    """A single response template for an explicit route."""

    status: int = 200
    headers: Dict[str, Any] = field(default_factory=dict)
    body: Any = None
    file: Optional[str] = None


@dataclass
class RouteSpec:
    """An explicit endpoint: method + path + optional matchers + response."""

    method: str
    path: str
    match: Dict[str, Any]
    priority: int
    latency: Optional[Dict[str, Any]]
    chaos: Optional[Dict[str, Any]]
    response: ResponseSpec
    regex: Pattern[str]
    param_names: List[str]
    order: int = 0


@dataclass
class ResourceSpec:
    """An in-memory CRUD collection."""

    name: str
    path: str
    id_field: str = "id"
    id_type: str = "int"  # "int" | "uuid"
    seed: List[Dict[str, Any]] = field(default_factory=list)
    latency: Optional[Dict[str, Any]] = None
    chaos: Optional[Dict[str, Any]] = None


@dataclass
class MockConfig:
    """The whole parsed mocks file."""

    settings: Dict[str, Any] = field(default_factory=dict)
    routes: List[RouteSpec] = field(default_factory=list)
    resources: List[ResourceSpec] = field(default_factory=list)
    record: Optional[Dict[str, Any]] = None
    base_dir: str = "."

    @property
    def seed(self) -> Optional[int]:
        return self.settings.get("seed")

    @property
    def cors(self) -> bool:
        return bool(self.settings.get("cors", True))

    @property
    def global_latency(self) -> Optional[Dict[str, Any]]:
        return self.settings.get("latency")

    @property
    def global_chaos(self) -> Optional[Dict[str, Any]]:
        return self.settings.get("chaos")


def _build_route(raw: Dict[str, Any], order: int) -> RouteSpec:
    method = str(raw.get("method", "GET")).upper()
    path = str(raw.get("path", "/"))
    regex, names = compile_path(path)

    resp_raw = raw.get("response") or {}
    response = ResponseSpec(
        status=int(resp_raw.get("status", 200)),
        headers=dict(resp_raw.get("headers") or {}),
        body=resp_raw.get("body"),
        file=resp_raw.get("file"),
    )
    return RouteSpec(
        method=method,
        path=path,
        match=dict(raw.get("match") or {}),
        priority=int(raw.get("priority", 0)),
        latency=raw.get("latency"),
        chaos=raw.get("chaos"),
        response=response,
        regex=regex,
        param_names=names,
        order=order,
    )


def _build_resource(raw: Dict[str, Any]) -> ResourceSpec:
    name = str(raw["name"])
    path = str(raw.get("path", "/" + name))
    return ResourceSpec(
        name=name,
        path=path.rstrip("/") or "/",
        id_field=str(raw.get("id_field", "id")),
        id_type=str(raw.get("id_type", "int")),
        seed=list(raw.get("seed") or []),
        latency=raw.get("latency"),
        chaos=raw.get("chaos"),
    )


def build_config(data: Optional[Dict[str, Any]], base_dir: str = ".") -> MockConfig:
    """Build a :class:`MockConfig` from an already-parsed mapping."""
    data = data or {}
    routes = [_build_route(r, i) for i, r in enumerate(data.get("routes") or [])]
    resources = [_build_resource(r) for r in data.get("resources") or []]
    return MockConfig(
        settings=dict(data.get("config") or {}),
        routes=routes,
        resources=resources,
        record=data.get("record"),
        base_dir=base_dir,
    )


def load_config(path: str) -> MockConfig:
    """Load and parse a mocks YAML file from disk."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    base_dir = os.path.dirname(os.path.abspath(path))
    return build_config(data, base_dir=base_dir)
