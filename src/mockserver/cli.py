"""Command-line interface.

    mockserver serve    [--config mocks.yaml] [--host] [--port] [--seed] [--fixtures DIR] [--watch]
    mockserver replay   [--fixtures DIR] [--config mocks.yaml] [--host] [--port] [--seed]
    mockserver record   [--upstream URL] [--fixtures DIR] [--config] [--host] [--port] [--seed]
    mockserver validate FILE [FILE ...] [--strict] [--json]
    mockserver import-openapi <spec> [--out mocks.yaml]
    mockserver --version

``serve``, ``replay`` and ``record`` validate the mocks file, then boot the
ASGI app with uvicorn; they refuse to start on validation errors unless
``--no-validate`` is given. ``validate`` only checks files (exit 1 on errors),
which makes it a one-line CI step. ``import-openapi`` writes a mocks file from
an OpenAPI document and exits.

Defaults come from, highest priority first: the command-line flag, a real
environment variable, a ``.env`` file in the current directory (or the one
given with ``--env-file``), then the built-in default. The variables are:

    MOCK_CONFIG    mocks file for serve/replay/record     (default mocks.yaml)
    MOCK_HOST      bind host                              (default 127.0.0.1)
    MOCK_PORT      bind port                              (default 8000)
    MOCK_SEED      faker/chaos seed override              (default: the file's)
    MOCK_UPSTREAM  upstream for `record`
    MOCK_FIXTURES  fixtures directory for record/replay   (default fixtures)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import __version__
from .config import MockConfig, build_config, load_config
from .openapi_import import import_openapi_to_yaml
from .server import create_app
from .validate import Problem, format_report, has_errors, validate_file

ENV_VARS = ("MOCK_CONFIG", "MOCK_HOST", "MOCK_PORT", "MOCK_SEED", "MOCK_UPSTREAM", "MOCK_FIXTURES")
DEFAULT_ENV_FILE = ".env"


# --------------------------------------------------------------------------- #
# .env handling (no python-dotenv dependency)
# --------------------------------------------------------------------------- #

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``KEY=value`` lines the way most .env tools do.

    Supports comments, blank lines, ``export KEY=...``, single quotes
    (literal), double quotes (with ``\\n``-style escapes) and ``# comments``
    after unquoted values.
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            while quote == '"' and end > 0 and value[end - 1] == "\\":
                end = value.find(quote, end + 1)
            inner = value[1:end] if end > 0 else value[1:]
            if quote == '"':
                inner = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)), inner)
            value = inner
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        out[key] = value
    return out


def load_dotenv_file(path: str) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_dotenv(fh.read())


class EnvDefaults:
    """Resolves MOCK_* defaults: real environment beats the .env file."""

    def __init__(self, dotenv: Optional[Mapping[str, str]] = None, environ: Optional[Mapping[str, str]] = None) -> None:
        env = os.environ if environ is None else environ
        self.values: Dict[str, str] = {}
        for source in (dotenv or {}, env):
            for key, value in source.items():
                if key in ENV_VARS and value is not None and str(value).strip() != "":
                    self.values[key] = str(value).strip()

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(name, default)

    def get_int(self, name: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.values.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(f"error: {name}={raw!r} is not an integer") from None

    def has(self, name: str) -> bool:
        return name in self.values


def _preparse_env_file(argv: List[str]) -> Optional[str]:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file")
    known, _ = pre.parse_known_args(argv)
    return known.env_file


def resolve_env(argv: List[str], environ: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> EnvDefaults:
    """Find and load the .env file named by --env-file (or ./.env if present)."""
    explicit = _preparse_env_file(argv)
    base = cwd or os.getcwd()
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(base, explicit)
        if not os.path.isfile(path):
            raise SystemExit(f"error: --env-file {explicit} does not exist")
        return EnvDefaults(load_dotenv_file(path), environ)
    path = os.path.join(base, DEFAULT_ENV_FILE)
    dotenv = load_dotenv_file(path) if os.path.isfile(path) else {}
    return EnvDefaults(dotenv, environ)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _run(app: Any, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency guard
        sys.stderr.write(
            "uvicorn is required to run the server. Install it with:\n"
            "    pip install 'uvicorn[standard]'\n"
        )
        raise SystemExit(1)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _configure_logging() -> None:
    """Send mockserver's own log lines (reloads, mock errors) to stderr."""
    log = logging.getLogger("mockserver")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     [mockserver] %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def _check(path: str, skip: bool) -> bool:
    """Validate before booting. Returns False when the server must not start."""
    if skip:
        return True
    problems = validate_file(path)
    if has_errors(problems):
        sys.stderr.write(format_report(path, problems) + "\n")
        sys.stderr.write("Refusing to start. Fix the errors above, or pass --no-validate to start anyway.\n")
        return False
    for problem in problems:
        sys.stderr.write(f"{path}: {problem}\n")
    return True


def _load_optional_config(path: Optional[str], explicit: bool, skip_validation: bool = False) -> Optional[MockConfig]:
    """Load a config that may legitimately be absent (record/replay)."""
    if path and os.path.exists(path):
        if not _check(path, skip_validation):
            return None
        return load_config(path)
    if explicit and path:
        sys.stderr.write(f"Config file not found: {path}\n")
        return None
    return build_config({}, base_dir=os.getcwd())


def _banner(action: str, source: str, config: MockConfig, host: str, port: int) -> None:
    print(f"api-mock-server {action} {source} on http://{host}:{port}", flush=True)
    print(
        f"  routes: {len(config.routes)}  resources: {len(config.resources)}"
        f"  seed: {config.seed}  admin: http://{host}:{port}{config.admin_prefix}",
        flush=True,
    )


def _apply_seed(config: MockConfig, seed: Optional[int]) -> None:
    config.set_seed(seed)


def _cmd_serve(args: argparse.Namespace) -> int:
    if not os.path.exists(args.config):
        sys.stderr.write(f"Config file not found: {args.config}\n")
        return 1
    if not _check(args.config, args.no_validate):
        return 1
    config = load_config(args.config)
    _apply_seed(config, args.seed)
    if args.fixtures:
        fixtures = os.path.abspath(args.fixtures)
        config.set_record({"upstream": None, "fixtures_dir": fixtures, "record": False})
    _configure_logging()
    app = create_app(config, watch=args.watch)
    _banner("serving", args.config, config, args.host, args.port)
    if args.fixtures:
        print(f"  unmatched requests replay fixtures from: {config.record['fixtures_dir']}", flush=True)
    if args.watch:
        print("  watching the mocks file and its body files: edits apply on the next request", flush=True)
    _run(app, args.host, args.port)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    fixtures = os.path.abspath(args.fixtures)
    if not os.path.isdir(fixtures):
        sys.stderr.write(
            f"Fixtures directory not found: {args.fixtures}\n"
            "Record some first:  mockserver record --upstream https://api.example.com "
            f"--fixtures {args.fixtures}\n"
        )
        return 1
    config = _load_optional_config(args.config, explicit=args.config_explicit, skip_validation=args.no_validate)
    if config is None:
        return 1
    _apply_seed(config, args.seed)
    config.set_record({"upstream": None, "fixtures_dir": fixtures, "record": False})
    _configure_logging()
    app = create_app(config)
    recorder = app.state.mock_server.recorder
    _banner("replaying", fixtures, config, args.host, args.port)
    print(f"  fixtures loaded: {recorder.fixture_count}  (offline: no upstream is contacted)", flush=True)
    _run(app, args.host, args.port)
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    if not args.upstream:
        sys.stderr.write(
            "record needs an upstream: pass --upstream URL or set MOCK_UPSTREAM.\n"
            "To serve saved fixtures offline instead, run:  mockserver replay "
            f"--fixtures {args.fixtures}\n"
        )
        return 2
    config = _load_optional_config(args.config, explicit=args.config_explicit, skip_validation=args.no_validate)
    if config is None:
        return 1
    _apply_seed(config, args.seed)
    fixtures = os.path.abspath(args.fixtures)
    config.set_record({"upstream": args.upstream, "fixtures_dir": fixtures, "record": True})
    _configure_logging()
    app = create_app(config)
    _banner("recording", args.upstream, config, args.host, args.port)
    print(f"  unmatched requests are proxied and saved to: {fixtures}", flush=True)
    _run(app, args.host, args.port)
    return 0


def _summary(path: str) -> str:
    try:
        config = load_config(path)
    except Exception:  # noqa: BLE001 - the summary is cosmetic
        return ""
    routes, resources = len(config.routes), len(config.resources)
    return (f"{routes} route{'s' if routes != 1 else ''}, "
            f"{resources} resource{'s' if resources != 1 else ''}")


def _cmd_validate(args: argparse.Namespace) -> int:
    failed = False
    results: List[Dict[str, Any]] = []
    for path in args.files:
        problems: List[Problem] = validate_file(path)
        bad = has_errors(problems) or (args.strict and bool(problems))
        failed = failed or bad
        if args.json:
            results.append({"file": path, "ok": not bad, "problems": [p.to_dict() for p in problems]})
        else:
            summary = _summary(path) if not has_errors(problems) else ""
            print(format_report(path, problems, summary))
    if args.json:
        print(json.dumps(results, indent=2))
    return 1 if failed else 0


def _cmd_import(args: argparse.Namespace) -> int:
    if not os.path.exists(args.spec):
        sys.stderr.write(f"OpenAPI spec not found: {args.spec}\n")
        return 1
    count = import_openapi_to_yaml(args.spec, args.out)
    print(f"Imported {count} route(s) from {args.spec} -> {args.out}")
    print(f"Serve it with:  mockserver serve --config {args.out}")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

class _Explicit(argparse.Action):
    """Store the value and remember that the user typed the flag."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: Optional[str] = None) -> None:
        setattr(namespace, self.dest, values)
        setattr(namespace, self.dest + "_explicit", True)


def build_parser(env: Optional[EnvDefaults] = None) -> argparse.ArgumentParser:
    env = env or EnvDefaults({}, {})
    host = env.get("MOCK_HOST", "127.0.0.1")
    port = env.get_int("MOCK_PORT", 8000)
    seed = env.get_int("MOCK_SEED", None)
    config_default = env.get("MOCK_CONFIG", "mocks.yaml")
    fixtures_default = env.get("MOCK_FIXTURES", "fixtures")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="Read MOCK_* defaults from this file instead of ./.env.",
    )

    def add_bind(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default=host, help=f"Bind host (MOCK_HOST, default {host}).")
        p.add_argument("--port", "-p", type=int, default=port, help=f"Bind port (MOCK_PORT, default {port}).")
        p.add_argument("--seed", type=int, default=seed, help="Override the chaos/faker seed (MOCK_SEED).")
        p.add_argument("--no-validate", action="store_true",
                       help="Start even if the mocks file has validation errors.")

    parser = argparse.ArgumentParser(
        prog="mockserver",
        description="A config-driven mock REST API for frontend dev and testing.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"api-mock-server {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve = sub.add_parser("serve", parents=[common], help="Serve endpoints from a mocks file.")
    serve.add_argument("--config", "-c", default=config_default,
                       help=f"Path to the mocks YAML file (MOCK_CONFIG, default {config_default}).")
    add_bind(serve)
    serve.add_argument("--fixtures", default=None, metavar="DIR",
                       help="Also replay recorded fixtures from DIR for unmatched requests (offline).")
    serve.add_argument("--watch", action="store_true",
                       help="Reload the mocks file (and body files) when they change, without a restart.")
    serve.set_defaults(func=_cmd_serve)

    replay = sub.add_parser("replay", parents=[common],
                            help="Serve recorded fixtures offline, with no upstream.")
    replay.add_argument("--fixtures", default=fixtures_default, metavar="DIR",
                        help=f"Fixtures directory (MOCK_FIXTURES, default {fixtures_default}).")
    replay.add_argument("--config", "-c", default=config_default, action=_Explicit,
                        help="Optional mocks file whose routes/resources are served first.")
    add_bind(replay)
    replay.set_defaults(func=_cmd_replay, config_explicit=False)

    rec = sub.add_parser("record", parents=[common],
                         help="Proxy unmatched requests to an upstream and record them.")
    rec.add_argument("--upstream", "-u", default=env.get("MOCK_UPSTREAM"),
                     help="Upstream base URL to proxy to (MOCK_UPSTREAM).")
    rec.add_argument("--config", "-c", default=config_default, action=_Explicit,
                     help="Optional mocks file served first.")
    rec.add_argument("--fixtures", default=fixtures_default, metavar="DIR",
                     help=f"Directory to store recorded fixtures (MOCK_FIXTURES, default {fixtures_default}).")
    add_bind(rec)
    rec.set_defaults(func=_cmd_record, config_explicit=False)

    val = sub.add_parser("validate", parents=[common],
                         help="Check mocks files for typos and invalid values (exit 1 on errors).")
    val.add_argument("files", nargs="+", metavar="FILE", help="Mocks files to check.")
    val.add_argument("--strict", action="store_true", help="Treat warnings as errors too.")
    val.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    val.set_defaults(func=_cmd_validate)

    imp = sub.add_parser("import-openapi", parents=[common], help="Build a mocks file from an OpenAPI spec.")
    imp.add_argument("spec", help="Path to the OpenAPI 3 document (YAML or JSON).")
    imp.add_argument("--out", "-o", default="mocks.yaml", help="Output mocks file.")
    imp.set_defaults(func=_cmd_import)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        env = resolve_env(args_list)
        parser = build_parser(env)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            sys.stderr.write(exc.code + "\n")
            return 2
        raise
    args = parser.parse_args(args_list)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
