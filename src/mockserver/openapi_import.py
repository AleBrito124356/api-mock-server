"""Build a mocks.yaml skeleton from an OpenAPI 3 spec.

For every operation the importer picks the most useful documented response
(prefer 200, then any 2xx, then ``default``) and turns it into a mock route.
The response body comes from, in order of preference:

  1. a response ``example`` / ``examples``,
  2. a schema ``example``,
  3. a value synthesized from the schema (respecting ``example``, ``default``,
     ``enum`` and ``format`` at every level).

``$ref`` pointers into ``#/components`` are resolved. The output is a plain
dict ready to hand to :func:`mockserver.config.build_config` or to dump with
``yaml.safe_dump``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import yaml

_MAX_DEPTH = 8


class _RefResolver:
    def __init__(self, root: Dict[str, Any]) -> None:
        self.root = root

    def resolve(self, node: Any, _seen: Optional[set] = None) -> Any:
        seen = _seen or set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return {}
            seen.add(ref)
            node = self._lookup(ref)
        return node

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return {}
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node


def _string_for_format(schema: Dict[str, Any]) -> str:
    fmt = schema.get("format")
    mapping = {
        "date-time": "2026-01-01T00:00:00Z",
        "date": "2026-01-01",
        "time": "00:00:00",
        "email": "user@example.com",
        "uuid": "00000000-0000-0000-0000-000000000000",
        "uri": "https://example.com",
        "hostname": "example.com",
        "ipv4": "192.0.2.1",
        "byte": "c3RyaW5n",
        "password": "string",
    }
    if fmt in mapping:
        return mapping[fmt]
    if "pattern" in schema:
        return schema["pattern"]
    return "string"


def example_from_schema(schema: Any, resolver: _RefResolver, depth: int = 0) -> Any:
    schema = resolver.resolve(schema)
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    for combiner in ("allOf", "oneOf", "anyOf"):
        if combiner in schema and schema[combiner]:
            if combiner == "allOf":
                merged: Dict[str, Any] = {}
                for sub in schema[combiner]:
                    part = example_from_schema(sub, resolver, depth + 1)
                    if isinstance(part, dict):
                        merged.update(part)
                if merged:
                    return merged
            return example_from_schema(schema[combiner][0], resolver, depth + 1)

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties", {}) or {}
        return {
            name: example_from_schema(sub, resolver, depth + 1)
            for name, sub in props.items()
        }
    if schema_type == "array":
        item = example_from_schema(schema.get("items", {}), resolver, depth + 1)
        return [item] if item is not None else []
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return True
    if schema_type == "string":
        return _string_for_format(schema)
    if schema_type == "null":
        return None
    # Untyped schema: best effort.
    if "properties" in schema:
        return {n: example_from_schema(s, resolver, depth + 1) for n, s in schema["properties"].items()}
    return None


def _pick_response(responses: Dict[str, Any]) -> Optional[tuple]:
    if not responses:
        return None
    order: List[str] = []
    if "200" in responses:
        order.append("200")
    for code in responses:
        if code.startswith("2") and code not in order:
            order.append(code)
    if "default" in responses:
        order.append("default")
    for code in responses:
        if code not in order:
            order.append(code)
    code = order[0]
    status = 200 if code == "default" else int(code)
    return code, status, responses[code]


def _body_from_response(response: Dict[str, Any], resolver: _RefResolver) -> Any:
    response = resolver.resolve(response)
    content = response.get("content", {}) or {}
    media = content.get("application/json")
    if media is None and content:
        media = next(iter(content.values()))
    if not media:
        return None
    media = resolver.resolve(media)
    if "example" in media:
        return media["example"]
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        first = resolver.resolve(next(iter(examples.values())))
        if isinstance(first, dict) and "value" in first:
            return first["value"]
    if "schema" in media:
        return example_from_schema(media["schema"], resolver)
    return None


def import_openapi(spec_path: str) -> Dict[str, Any]:
    """Parse an OpenAPI 3 file and return a mocks-config dict."""
    with open(spec_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        spec = yaml.safe_load(raw)
    except yaml.YAMLError:
        spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("The OpenAPI document did not parse to an object.")

    resolver = _RefResolver(spec)
    routes: List[Dict[str, Any]] = []
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}

    for path, item in (spec.get("paths") or {}).items():
        item = resolver.resolve(item)
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in methods or not isinstance(operation, dict):
                continue
            picked = _pick_response(operation.get("responses") or {})
            status = 200
            body: Any = None
            if picked:
                _code, status, response = picked
                body = _body_from_response(response, resolver)
            route: Dict[str, Any] = {
                "method": method.upper(),
                "path": path,
                "response": {
                    "status": status,
                    "headers": {"Content-Type": "application/json"},
                    "body": body,
                },
            }
            summary = operation.get("summary") or operation.get("operationId")
            if summary:
                route["description"] = str(summary)
            routes.append(route)

    info = spec.get("info", {}) or {}
    config: Dict[str, Any] = {
        "config": {
            "cors": True,
            "seed": 42,
            "_source": f"imported from {os.path.basename(spec_path)}: "
                       f"{info.get('title', 'API')} {info.get('version', '')}".strip(),
        },
        "routes": routes,
    }
    return config


def import_openapi_to_yaml(spec_path: str, out_path: str) -> int:
    """Import a spec and write mocks YAML. Returns the number of routes."""
    config = import_openapi(spec_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# Generated by: mockserver import-openapi\n")
        fh.write(f"# Source: {os.path.basename(spec_path)}\n")
        fh.write("# Review the bodies below - they come from schema examples.\n\n")
        yaml.safe_dump(config, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return len(config["routes"])
