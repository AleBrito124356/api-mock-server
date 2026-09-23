"""api-mock-server: a config-driven mock REST API for frontend dev and testing.

Public surface:
    load_config(path)        -> MockConfig loaded from a YAML file
    build_config(data, dir)  -> MockConfig from an already-parsed dict
    create_app(config)       -> a Starlette ASGI app that serves the mocks
"""
from .config import MockConfig, RouteSpec, ResourceSpec, ResponseSpec, build_config, load_config
from .server import create_app

__version__ = "0.2.0"

__all__ = [
    "MockConfig",
    "RouteSpec",
    "ResourceSpec",
    "ResponseSpec",
    "build_config",
    "load_config",
    "create_app",
    "__version__",
]
