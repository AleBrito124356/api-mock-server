"""Shared test fixtures and path setup.

Puts ``src`` on sys.path so the tests run against the working tree without an
editable install, and exposes a small helper to build an httpx client bound to
an app via ASGITransport.
"""
import os
import sys

import httpx
import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def make_client(app) -> httpx.AsyncClient:
    """Return an AsyncClient that speaks to the ASGI app in-process."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://mock.test")


@pytest.fixture
def client_factory():
    return make_client
