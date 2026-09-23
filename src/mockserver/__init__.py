"""api-mock-server: a config-driven mock REST API for frontend dev and testing.

Public surface:
    load_config(path, strict=False) -> MockConfig loaded from a YAML file
                                       (strict=True raises ConfigError on problems)
    build_config(data, dir)         -> MockConfig from an already-parsed dict
    create_app(config, ...)         -> a Starlette ASGI app that serves the mocks
                                       (watch=True for hot reload,
                                        upstream_transport= for record mode tests)
    validate_file(path)             -> list of Problem found in a mocks file
    import_openapi(path, ...)       -> mocks-config dict from an OpenAPI document
"""
from .config import MockConfig, ResourceSpec, ResponseSpec, RouteSpec, build_config, load_config
from .openapi_import import import_openapi
from .server import create_app
from .validate import ConfigError, Problem, validate_data, validate_file

__version__ = "0.2.0"

__all__ = [
    "MockConfig",
    "RouteSpec",
    "ResourceSpec",
    "ResponseSpec",
    "ConfigError",
    "Problem",
    "build_config",
    "load_config",
    "create_app",
    "validate_data",
    "validate_file",
    "import_openapi",
    "__version__",
]
