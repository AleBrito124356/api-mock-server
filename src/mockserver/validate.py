"""Static validation of a mocks file.

A mocks file is meant to be edited by people who do not read the source, so a
typo must be reported, not silently turned into a wrong mock. Without this,
``respnse:`` on a route produced a 200 with an empty body and ``{{ faker.nmae }}``
rendered ``null``.

:func:`validate_data` walks a parsed mocks mapping and returns a list of
:class:`Problem` objects, each with a JSON-pointer-like location such as
``routes[3].response.status`` and, for unknown keys or names, a
"did you mean" suggestion. It checks:

* known keys at every level (keys starting with ``x-`` or ``_`` are free-form
  extensions and are ignored),
* HTTP methods, status codes (100-599), priorities,
* path syntax and ``{param}`` names,
* latency / chaos / rate-limit types and ranges,
* resource names, paths, id settings and seed data,
* that ``response.file`` exists (relative to the mocks file) and parses when
  it is JSON,
* every ``{{ ... }}`` expression: known roots, request scopes, path params the
  route really captures, faker helpers and their arguments, filters.

Errors make ``mockserver serve`` refuse to start (``--no-validate`` skips
this); warnings are printed but do not block.
"""
from __future__ import annotations

import difflib
import inspect
import json
import os
import re
from dataclasses import dataclass
from random import Random
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

from .dynamic import (
    KNOWN_FILTERS,
    KNOWN_ROOTS,
    NOW_PARTS,
    REQUEST_SCOPES,
    MockFaker,
    faker_helpers,
    find_expressions,
    parse_expression,
)

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY", "*")

TOP_KEYS = ("config", "routes", "resources", "record")
CONFIG_KEYS = ("seed", "cors", "latency", "chaos")
ROUTE_KEYS = ("name", "description", "method", "path", "priority", "match", "latency", "chaos", "response")
RESPONSE_KEYS = ("status", "headers", "body", "file")
MATCH_KEYS = ("query", "headers", "body")
LATENCY_KEYS = ("fixed_ms", "random_ms", "min_ms", "max_ms")
CHAOS_KEYS = ("error_rate", "error_status", "error_body", "rate_limit")
RATE_LIMIT_KEYS = ("limit", "window_ms", "status", "body")
RESOURCE_KEYS = ("name", "description", "path", "id_field", "id_type", "seed", "latency", "chaos")
RECORD_KEYS = ("upstream", "fixtures_dir", "record")
ID_TYPES = ("int", "uuid")

_PARAM = re.compile(r"\{([^{}]*)\}")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Problem:
    """One finding. ``level`` is ``"error"`` or ``"warning"``."""

    level: str
    location: str
    message: str

    def __str__(self) -> str:
        where = f"{self.location}: " if self.location else ""
        return f"{self.level:<8} {where}{self.message}"

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "location": self.location, "message": self.message}


class ConfigError(ValueError):
    """Raised by ``load_config(..., strict=True)`` when validation finds errors."""

    def __init__(self, source: str, problems: List[Problem]) -> None:
        self.source = source
        self.problems = problems
        errors = [p for p in problems if p.level == "error"]
        lines = "\n  ".join(str(p) for p in errors)
        super().__init__(f"{source}: {len(errors)} error(s)\n  {lines}")


def has_errors(problems: Iterable[Problem]) -> bool:
    return any(p.level == "error" for p in problems)


def _suggest(word: str, options: Sequence[str]) -> str:
    match = difflib.get_close_matches(str(word), list(options), n=1, cutoff=0.6)
    return f" (did you mean '{match[0]}'?)" if match else ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


class _Validator:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.problems: List[Problem] = []
        self._helpers = faker_helpers()
        self._probe = MockFaker(Random(0))

    # -- reporting -------------------------------------------------------- #
    def error(self, loc: str, msg: str) -> None:
        self.problems.append(Problem("error", loc, msg))

    def warn(self, loc: str, msg: str) -> None:
        self.problems.append(Problem("warning", loc, msg))

    # -- generic helpers -------------------------------------------------- #
    def mapping(self, value: Any, loc: str, what: str = "a mapping") -> bool:
        if not isinstance(value, dict):
            self.error(loc, f"must be {what}, got {type(value).__name__}")
            return False
        return True

    def keys(self, value: Dict[str, Any], allowed: Sequence[str], loc: str) -> None:
        for key in value:
            k = str(key)
            if k.startswith("x-") or k.startswith("_"):
                continue
            if k not in allowed:
                self.error(loc, f"unknown key '{k}'{_suggest(k, allowed)}")

    def status(self, value: Any, loc: str, allow_template: bool = False) -> None:
        if allow_template and _is_template(value):
            return
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if not _is_int(value) or not 100 <= value <= 599:
            self.error(loc, f"{value!r} is not a valid HTTP status (an integer 100-599)")

    def non_negative(self, value: Any, loc: str) -> bool:
        if not _is_number(value) or value < 0:
            self.error(loc, f"must be a number >= 0, got {value!r}")
            return False
        return True

    # -- template checks -------------------------------------------------- #
    def templates(self, value: Any, loc: str, params: Optional[Sequence[str]]) -> None:
        """Check every {{ }} expression inside a (possibly nested) value."""
        if isinstance(value, str):
            self._template_string(value, loc, params)
        elif isinstance(value, dict):
            for k, v in value.items():
                self.templates(v, f"{loc}.{k}", params)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.templates(v, f"{loc}[{i}]", params)

    def _template_string(self, text: str, loc: str, params: Optional[Sequence[str]]) -> None:
        if text.count("{{") != len(find_expressions(text)):
            self.warn(loc, "has a '{{' without a matching '}}'; it is sent as literal text")
        for expr in find_expressions(text):
            self._expression(expr, loc, params)

    def _expression(self, expr: str, loc: str, params: Optional[Sequence[str]]) -> None:
        where = f"{loc}: '{{{{ {expr} }}}}'"
        if not expr:
            self.error(loc, "empty '{{ }}' expression")
            return
        parsed = parse_expression(expr)
        for bad in parsed.bad_filters:
            self.error(loc, f"cannot parse filter '{bad}' in '{{{{ {expr} }}}}'")
        for name, _args in parsed.filters:
            if name not in KNOWN_FILTERS:
                self.error(loc, f"unknown filter '{name}'{_suggest(name, KNOWN_FILTERS)} in '{{{{ {expr} }}}}'")
        if parsed.kind == "literal":
            return
        root, rest = parsed.root or "", parsed.rest
        if root not in KNOWN_ROOTS:
            self.error(
                loc,
                f"unknown template root '{root}'{_suggest(root, KNOWN_ROOTS)} in '{{{{ {expr} }}}}' "
                f"(known: {', '.join(KNOWN_ROOTS)}; quote text like {{{{ 'text' }}}})",
            )
            return
        if root == "request":
            self._request_ref(rest, where, loc, expr, params)
        elif root == "faker":
            self._faker_ref(rest, parsed.args, loc, expr)
        elif root == "seq":
            if not rest:
                self.error(loc, f"'seq' needs a counter name, e.g. seq.order, in '{{{{ {expr} }}}}'")
            elif any(not _is_int(a) for a in parsed.args) or len(parsed.args) > 2:
                self.error(loc, f"seq takes up to two integers (start, step) in '{{{{ {expr} }}}}'")
        elif root == "now":
            if rest and rest[0] not in NOW_PARTS:
                self.error(loc, f"unknown now part '{rest[0]}'{_suggest(rest[0], NOW_PARTS)} in '{{{{ {expr} }}}}'")
        elif root == "env":
            if not rest and not parsed.args:
                self.error(loc, f"'env' needs a variable name, e.g. env.API_URL, in '{{{{ {expr} }}}}'")
        elif root == "uuid":
            if rest:
                self.warn(loc, f"'uuid' takes no attribute; '.{'.'.join(rest)}' is ignored in '{{{{ {expr} }}}}'")

    def _request_ref(self, rest: List[str], where: str, loc: str, expr: str,
                     params: Optional[Sequence[str]]) -> None:
        if not rest:
            self.error(loc, f"'request' needs a scope ({', '.join(REQUEST_SCOPES)}) in '{{{{ {expr} }}}}'")
            return
        scope = rest[0]
        if scope not in REQUEST_SCOPES:
            self.error(loc, f"unknown request scope '{scope}'{_suggest(scope, REQUEST_SCOPES)} in '{{{{ {expr} }}}}'")
            return
        if scope in ("query", "header", "headers", "path") and len(rest) < 2:
            self.error(loc, f"'request.{scope}' needs a name, e.g. request.{scope}.id, in '{{{{ {expr} }}}}'")
            return
        if scope == "path" and params is not None:
            name = rest[1]
            if name not in params:
                have = ", ".join("{%s}" % p for p in params) or "no params"
                self.error(
                    loc,
                    f"path param '{name}'{_suggest(name, params)} is not captured by this route ({have}) "
                    f"in '{{{{ {expr} }}}}'",
                )

    def _faker_ref(self, rest: List[str], args: List[Any], loc: str, expr: str) -> None:
        if not rest:
            self.error(loc, f"'faker' needs a helper name in '{{{{ {expr} }}}}'")
            return
        helper = rest[0]
        if helper not in self._helpers:
            self.error(loc, f"unknown faker helper '{helper}'{_suggest(helper, self._helpers)} in '{{{{ {expr} }}}}'")
            return
        func = getattr(MockFaker, helper)
        try:
            inspect.signature(func).bind(None, *args)
        except TypeError as exc:
            self.error(loc, f"faker.{helper}: {exc} in '{{{{ {expr} }}}}'")
            return
        try:
            getattr(self._probe, helper)(*args)
        except (TypeError, ValueError) as exc:
            self.error(loc, f"faker.{helper}{tuple(args)!r} fails: {exc} in '{{{{ {expr} }}}}'")

    # -- sections --------------------------------------------------------- #
    def top(self, data: Any) -> None:
        if data is None:
            self.warn("", "the file is empty: nothing will be served")
            return
        if not self.mapping(data, "", "a mapping with config/routes/resources/record"):
            return
        self.keys(data, TOP_KEYS, "")
        if "config" in data and data["config"] is not None:
            self.config(data["config"], "config")
        routes = data.get("routes")
        if routes is not None:
            if isinstance(routes, list):
                self.routes(routes)
            else:
                self.error("routes", f"must be a list, got {type(routes).__name__}")
        resources = data.get("resources")
        if resources is not None:
            if isinstance(resources, list):
                self.resources(resources)
            else:
                self.error("resources", f"must be a list, got {type(resources).__name__}")
        if data.get("record") is not None:
            self.record(data["record"], "record")
        if not routes and not resources and not data.get("record"):
            self.warn("", "no routes, resources or record block: every request will 404")

    def config(self, cfg: Any, loc: str) -> None:
        if not self.mapping(cfg, loc):
            return
        self.keys(cfg, CONFIG_KEYS, loc)
        seed = cfg.get("seed")
        if seed is not None and not _is_int(seed):
            self.error(f"{loc}.seed", f"must be an integer, got {seed!r}")
        if "cors" in cfg and not isinstance(cfg["cors"], bool):
            self.error(f"{loc}.cors", f"must be true or false, got {cfg['cors']!r}")
        if cfg.get("latency") is not None:
            self.latency(cfg["latency"], f"{loc}.latency")
        if cfg.get("chaos") is not None:
            self.chaos(cfg["chaos"], f"{loc}.chaos", params=[])

    def latency(self, spec: Any, loc: str) -> None:
        if not self.mapping(spec, loc):
            return
        self.keys(spec, LATENCY_KEYS, loc)
        modes = [k for k in ("fixed_ms", "random_ms") if k in spec]
        if "min_ms" in spec or "max_ms" in spec:
            modes.append("min_ms/max_ms")
        if len(modes) > 1:
            self.warn(loc, f"several delay modes set ({', '.join(modes)}); only the first of fixed_ms, "
                           "random_ms, min_ms/max_ms is used")
        if "fixed_ms" in spec:
            self.non_negative(spec["fixed_ms"], f"{loc}.fixed_ms")
        if "random_ms" in spec:
            rng = spec["random_ms"]
            if (not isinstance(rng, list) or len(rng) != 2
                    or not all(_is_number(v) and v >= 0 for v in rng)):
                self.error(f"{loc}.random_ms", f"must be [low, high] with numbers >= 0, got {rng!r}")
            elif rng[0] > rng[1]:
                self.error(f"{loc}.random_ms", f"low {rng[0]} is greater than high {rng[1]}")
        lo, hi = spec.get("min_ms"), spec.get("max_ms")
        ok_lo = lo is None or self.non_negative(lo, f"{loc}.min_ms")
        ok_hi = hi is None or self.non_negative(hi, f"{loc}.max_ms")
        if ok_lo and ok_hi and lo is not None and hi is not None and lo > hi:
            self.error(loc, f"min_ms {lo} is greater than max_ms {hi}")

    def chaos(self, spec: Any, loc: str, params: Optional[Sequence[str]]) -> None:
        if not self.mapping(spec, loc):
            return
        self.keys(spec, CHAOS_KEYS, loc)
        if "error_rate" in spec:
            rate = spec["error_rate"]
            if not _is_number(rate) or not 0 <= rate <= 1:
                self.error(f"{loc}.error_rate", f"must be a number from 0 to 1, got {rate!r}")
        if "error_status" in spec:
            self.status(spec["error_status"], f"{loc}.error_status")
        if "error_body" in spec:
            self.templates(spec["error_body"], f"{loc}.error_body", params)
        if spec.get("rate_limit") is not None:
            self.rate_limit(spec["rate_limit"], f"{loc}.rate_limit", params)

    def rate_limit(self, spec: Any, loc: str, params: Optional[Sequence[str]]) -> None:
        if not self.mapping(spec, loc):
            return
        self.keys(spec, RATE_LIMIT_KEYS, loc)
        limit = spec.get("limit")
        if limit is None:
            self.error(f"{loc}.limit", "is required (requests allowed per window)")
        elif not _is_int(limit) or limit < 1:
            self.error(f"{loc}.limit", f"must be an integer >= 1, got {limit!r}")
        if "window_ms" in spec and (not _is_number(spec["window_ms"]) or spec["window_ms"] <= 0):
            self.error(f"{loc}.window_ms", f"must be a number > 0, got {spec['window_ms']!r}")
        if "status" in spec:
            self.status(spec["status"], f"{loc}.status")
        if "body" in spec:
            self.templates(spec["body"], f"{loc}.body", params)

    def path(self, path: Any, loc: str) -> Optional[List[str]]:
        """Check a route/resource path and return its param names."""
        if not isinstance(path, str) or not path:
            self.error(loc, f"must be a non-empty string, got {path!r}")
            return None
        if not path.startswith("/"):
            self.error(loc, f"must start with '/', got {path!r}")
        if path.count("{") != path.count("}"):
            self.error(loc, f"has unbalanced braces: {path!r}")
            return None
        names: List[str] = []
        for m in _PARAM.finditer(path):
            name = m.group(1)
            if not _IDENT.match(name):
                self.error(loc, f"path param '{{{name}}}' must be an identifier like {{id}}")
            elif name in names:
                self.error(loc, f"path param '{{{name}}}' appears twice")
            else:
                names.append(name)
        return names

    def routes(self, routes: List[Any]) -> None:
        names: Dict[str, int] = {}
        seen: Dict[str, int] = {}
        for i, route in enumerate(routes):
            loc = f"routes[{i}]"
            if not self.mapping(route, loc):
                continue
            self.keys(route, ROUTE_KEYS, loc)
            method = str(route.get("method", "GET")).upper()
            if method not in HTTP_METHODS:
                self.error(f"{loc}.method", f"unknown HTTP method '{route.get('method')}'"
                                            f"{_suggest(method, HTTP_METHODS)}")
            if "path" not in route:
                self.error(loc, "is missing 'path'")
                params: Optional[List[str]] = []
            else:
                params = self.path(route["path"], f"{loc}.path")
            name = route.get("name")
            if name is not None:
                if not isinstance(name, str) or not name.strip():
                    self.error(f"{loc}.name", f"must be a non-empty string, got {name!r}")
                elif name in names:
                    self.error(f"{loc}.name", f"'{name}' is already used by routes[{names[name]}]")
                else:
                    names[name] = i
            if "priority" in route and not _is_int(route["priority"]):
                self.error(f"{loc}.priority", f"must be an integer, got {route['priority']!r}")
            if route.get("match") is not None:
                self.match(route["match"], f"{loc}.match")
            if route.get("latency") is not None:
                self.latency(route["latency"], f"{loc}.latency")
            if route.get("chaos") is not None:
                self.chaos(route["chaos"], f"{loc}.chaos", params)
            self.route_responses(route, loc, params)

            signature = json.dumps(
                [method, route.get("path"), route.get("priority", 0), route.get("match") or {}],
                sort_keys=True, default=str,
            )
            if signature in seen:
                self.warn(loc, f"can never match: routes[{seen[signature]}] has the same method, path, "
                               "priority and match conditions and is tried first")
            else:
                seen[signature] = i

    def route_responses(self, route: Dict[str, Any], loc: str, params: Optional[Sequence[str]]) -> None:
        if "response" not in route:
            typo = difflib.get_close_matches("response", [str(k) for k in route], n=1, cutoff=0.6)
            if not typo:  # a misspelt 'response' key is already reported as an error
                self.warn(loc, "has no 'response': it answers 200 with an empty body")
            return
        if route["response"] is not None:
            self.response(route["response"], f"{loc}.response", params)

    def response(self, resp: Any, loc: str, params: Optional[Sequence[str]]) -> None:
        if not self.mapping(resp, loc):
            return
        self.keys(resp, RESPONSE_KEYS, loc)
        if "status" in resp:
            self.status(resp["status"], f"{loc}.status", allow_template=True)
            if _is_template(resp["status"]):
                self.templates(resp["status"], f"{loc}.status", params)
        headers = resp.get("headers")
        if headers is not None:
            if self.mapping(headers, f"{loc}.headers"):
                for key, value in headers.items():
                    if isinstance(value, (dict, list)):
                        self.error(f"{loc}.headers.{key}", "must be a single value, not a list or mapping")
                self.templates(headers, f"{loc}.headers", params)
        if "body" in resp:
            self.templates(resp["body"], f"{loc}.body", params)
        if resp.get("file") is not None:
            if "body" in resp and resp["body"] is not None:
                self.warn(loc, "has both 'file' and 'body'; the file wins and the body is ignored")
            self.body_file(resp["file"], f"{loc}.file", params)

    def body_file(self, file: Any, loc: str, params: Optional[Sequence[str]]) -> None:
        if not isinstance(file, str) or not file:
            self.error(loc, f"must be a file path, got {file!r}")
            return
        path = file if os.path.isabs(file) else os.path.join(self.base_dir, file)
        if not os.path.isfile(path):
            self.error(loc, f"file not found: {file} (looked in {os.path.dirname(os.path.abspath(path))})")
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            self.error(loc, f"cannot read {file}: {exc}")
            return
        if file.endswith(".json"):
            try:
                content: Any = json.loads(raw)
            except ValueError as exc:
                self.error(loc, f"{file} is not valid JSON: {exc}")
                return
            self.templates(content, f"{loc}<{file}>", params)
        else:
            self.templates(raw, f"{loc}<{file}>", params)

    def match(self, match: Any, loc: str) -> None:
        if not self.mapping(match, loc):
            return
        self.keys(match, MATCH_KEYS, loc)
        for part in ("query", "headers"):
            value = match.get(part)
            if value is None:
                continue
            if self.mapping(value, f"{loc}.{part}"):
                for key, expected in value.items():
                    if isinstance(expected, (dict, list)):
                        self.error(f"{loc}.{part}.{key}", "must be a single value (or \"*\" for any value)")

    def resources(self, resources: List[Any]) -> None:
        names: Dict[str, int] = {}
        paths: Dict[str, int] = {}
        for i, res in enumerate(resources):
            loc = f"resources[{i}]"
            if not self.mapping(res, loc):
                continue
            self.keys(res, RESOURCE_KEYS, loc)
            name = res.get("name")
            if not isinstance(name, str) or not name.strip():
                self.error(f"{loc}.name", "is required (a non-empty string)")
            elif name in names:
                self.error(f"{loc}.name", f"'{name}' is already used by resources[{names[name]}]")
            else:
                names[name] = i
            path = res.get("path", f"/{name}" if isinstance(name, str) else None)
            if path is not None:
                self.path(path, f"{loc}.path")
                norm = str(path).rstrip("/") or "/"
                if norm in paths:
                    self.error(f"{loc}.path", f"'{norm}' is already served by resources[{paths[norm]}]")
                else:
                    paths[norm] = i
            id_type = res.get("id_type", "int")
            if id_type not in ID_TYPES:
                self.error(f"{loc}.id_type", f"must be one of {', '.join(ID_TYPES)}, got {id_type!r}"
                                             f"{_suggest(str(id_type), ID_TYPES)}")
            id_field = res.get("id_field", "id")
            if not isinstance(id_field, str) or not id_field:
                self.error(f"{loc}.id_field", f"must be a non-empty string, got {id_field!r}")
                id_field = "id"
            seed = res.get("seed")
            if seed is not None:
                self.seed(seed, f"{loc}.seed", id_field)
            if res.get("latency") is not None:
                self.latency(res["latency"], f"{loc}.latency")
            if res.get("chaos") is not None:
                self.chaos(res["chaos"], f"{loc}.chaos", params=[])

    def seed(self, seed: Any, loc: str, id_field: str) -> None:
        if not isinstance(seed, list):
            self.error(loc, f"must be a list of items, got {type(seed).__name__}")
            return
        ids: Dict[str, int] = {}
        for j, item in enumerate(seed):
            if not isinstance(item, dict):
                self.error(f"{loc}[{j}]", f"must be a mapping, got {type(item).__name__}")
                continue
            if id_field in item:
                key = str(item[id_field])
                if key in ids:
                    self.error(f"{loc}[{j}].{id_field}", f"duplicate id {item[id_field]!r} (also {loc}[{ids[key]}])")
                else:
                    ids[key] = j

    def record(self, rec: Any, loc: str) -> None:
        if not self.mapping(rec, loc):
            return
        self.keys(rec, RECORD_KEYS, loc)
        upstream = rec.get("upstream")
        if upstream is not None and (not isinstance(upstream, str)
                                     or not re.match(r"^https?://", upstream)):
            self.error(f"{loc}.upstream", f"must be an http(s) URL or null, got {upstream!r}")
        fixtures = rec.get("fixtures_dir")
        if fixtures is not None and not isinstance(fixtures, str):
            self.error(f"{loc}.fixtures_dir", f"must be a directory path, got {fixtures!r}")
        if "record" in rec and not isinstance(rec["record"], bool):
            self.error(f"{loc}.record", f"must be true or false, got {rec['record']!r}")


def validate_data(data: Any, base_dir: str = ".") -> List[Problem]:
    """Validate an already-parsed mocks mapping."""
    validator = _Validator(base_dir)
    validator.top(data)
    return validator.problems


def validate_text(text: str, base_dir: str = ".") -> List[Problem]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        return [Problem("error", where, f"YAML syntax error: {problem}")]
    return validate_data(data, base_dir)


def validate_file(path: str) -> List[Problem]:
    """Read, parse and validate a mocks file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [Problem("error", "", f"cannot read {path}: {exc.strerror or exc}")]
    return validate_text(text, os.path.dirname(os.path.abspath(path)))


def format_report(source: str, problems: List[Problem], summary: str = "") -> str:
    """Human-readable report for one file."""
    errors = sum(1 for p in problems if p.level == "error")
    warnings = len(problems) - errors
    if not problems:
        return f"{source}: OK{f' ({summary})' if summary else ''}"
    counts = []
    if errors:
        counts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        counts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    ordered = sorted(problems, key=lambda p: 0 if p.level == "error" else 1)
    lines = [f"{source}: {', '.join(counts)}"] + [f"  {p}" for p in ordered]
    return "\n".join(lines)
