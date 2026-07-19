#!/usr/bin/env python
"""Entry point that works from a git checkout without installing the package.

Prefer the installed console script (``mockserver ...``) when the package is
installed. This wrapper just puts ``src`` on the path and delegates so the
CLI runs straight from the repo:

    python cli.py serve --config examples/shop-api/mocks.yaml
    python cli.py import-openapi examples/openapi-import/petstore.yaml -o mocks.yaml
    python cli.py record --upstream https://api.example.com --port 8000
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mockserver.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
