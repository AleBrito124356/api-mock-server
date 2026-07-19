"""Command-line interface.

    mockserver serve [--config mocks.yaml] [--host] [--port] [--seed]
    mockserver import-openapi <spec> [--out mocks.yaml]
    mockserver record --upstream <url> [--config] [--fixtures] [--port]

``serve`` and ``record`` boot the ASGI app with uvicorn. ``import-openapi``
writes a mocks skeleton from an OpenAPI 3 document and exits.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .config import build_config, load_config
from .openapi_import import import_openapi_to_yaml
from .server import create_app


def _run(app, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency guard
        sys.stderr.write(
            "uvicorn is required to run the server. Install it with:\n"
            "    pip install 'uvicorn[standard]'\n"
        )
        raise SystemExit(1)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _cmd_serve(args: argparse.Namespace) -> int:
    if not os.path.exists(args.config):
        sys.stderr.write(f"Config file not found: {args.config}\n")
        return 1
    config = load_config(args.config)
    if args.seed is not None:
        config.settings["seed"] = args.seed
    app = create_app(config)
    print(f"api-mock-server serving {args.config} on http://{args.host}:{args.port}")
    print(f"  routes: {len(config.routes)}  resources: {len(config.resources)}"
          f"  seed: {config.seed}")
    _run(app, args.host, args.port)
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    if not os.path.exists(args.spec):
        sys.stderr.write(f"OpenAPI spec not found: {args.spec}\n")
        return 1
    count = import_openapi_to_yaml(args.spec, args.out)
    print(f"Imported {count} route(s) from {args.spec} -> {args.out}")
    print(f"Serve it with:  mockserver serve --config {args.out}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    if args.config and os.path.exists(args.config):
        config = load_config(args.config)
    else:
        config = build_config({}, base_dir=os.getcwd())
    config.record = {
        "upstream": args.upstream,
        "fixtures_dir": args.fixtures,
        "record": True,
    }
    if args.seed is not None:
        config.settings["seed"] = args.seed
    app = create_app(config)
    print(f"api-mock-server recording {args.upstream} on http://{args.host}:{args.port}")
    print(f"  unmatched requests are proxied and saved to: {args.fixtures}")
    _run(app, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mockserver",
        description="A config-driven mock REST API for frontend dev and testing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Serve endpoints from a mocks file.")
    serve.add_argument("--config", "-c", default="mocks.yaml", help="Path to the mocks YAML file.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host.")
    serve.add_argument("--port", "-p", type=int, default=8000, help="Bind port.")
    serve.add_argument("--seed", type=int, default=None, help="Override the chaos/faker seed.")
    serve.set_defaults(func=_cmd_serve)

    imp = sub.add_parser("import-openapi", help="Build a mocks file from an OpenAPI 3 spec.")
    imp.add_argument("spec", help="Path to the OpenAPI 3 document (YAML or JSON).")
    imp.add_argument("--out", "-o", default="mocks.yaml", help="Output mocks file.")
    imp.set_defaults(func=_cmd_import)

    rec = sub.add_parser("record", help="Proxy unmatched requests to an upstream and record them.")
    rec.add_argument("--upstream", "-u", required=True, help="Upstream base URL to proxy to.")
    rec.add_argument("--config", "-c", default="mocks.yaml", help="Optional mocks file served first.")
    rec.add_argument("--fixtures", default="fixtures", help="Directory to store recorded fixtures.")
    rec.add_argument("--host", default="127.0.0.1", help="Bind host.")
    rec.add_argument("--port", "-p", type=int, default=8000, help="Bind port.")
    rec.add_argument("--seed", type=int, default=None, help="Override the chaos/faker seed.")
    rec.set_defaults(func=_cmd_record)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
