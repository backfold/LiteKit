from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import inspect
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from litestar import Controller, HttpMethod, Router
from litestar.handlers import BaseRouteHandler, HTTPRouteHandler

from litekit._layout import Layout
from litekit._segments import is_ignored, segment_to_path
from litekit.exceptions import LiteKitWarning, RouteConfigError

SERVER_FILE = "+server.py"
LAYOUT_FILE = "+layout.py"

HTTP_METHODS = frozenset(method.value for method in HttpMethod)


def file_router(directory: str | Path, *, path: str = "/") -> Router:
    """Build a Litestar ``Router`` from a SvelteKit-style routes directory.

    A relative ``directory`` is resolved against the file that calls this function,
    so the result does not depend on the working directory the app is started from.
    """
    root = Path(directory)
    if not root.is_absolute():
        root = Path(sys._getframe(1).f_code.co_filename).resolve().parent / root
    root = root.resolve()
    if not root.is_dir():
        raise RouteConfigError(f"Routes directory not found: {root}")

    build = _Build(root)
    package = "litekit_routes_" + hashlib.sha1(str(root).encode()).hexdigest()[:12]
    router = build.directory(root, package, (), path)
    build.check_conflicts()
    return router or Router(path=path, route_handlers=[])


@dataclass
class _Endpoint:
    segments: tuple[str, ...]
    method: str
    source: Path


@dataclass
class _Build:
    root: Path
    endpoints: list[_Endpoint] = field(default_factory=list)

    def directory(
        self, directory: Path, package: str, segments: tuple[str, ...], path: str
    ) -> Router | None:
        _ensure_package(package, directory)
        handlers: list[Any] = []

        server = directory / SERVER_FILE
        if server.is_file():
            for handler in _server_handlers(_load(server, package), self.relative(server)):
                handlers.append(handler)
                self.endpoints.extend(
                    _Endpoint(segments + extra, method, server)
                    for extra, method in _endpoints(handler)
                )

        for child in sorted(_subdirectories(directory)):
            segment = segment_to_path(child.name)
            if segment.endswith(":path}") and _subdirectories(child):
                raise RouteConfigError(
                    f"Rest parameter directory cannot have sub-routes: {self.relative(child)}"
                )
            sub = self.directory(
                child,
                f"{package}.{_module_name(child.name)}",
                segments + ((segment,) if segment else ()),
                f"/{segment}",
            )
            if sub is not None:
                handlers.append(sub)

        if not handlers:
            return None
        return Router(path=path, route_handlers=handlers, **self.layout_options(directory, package))

    def layout_options(self, directory: Path, package: str) -> dict[str, Any]:
        file = directory / LAYOUT_FILE
        if not file.is_file():
            return {}
        layout = getattr(_load(file, package), "layout", None)
        if not isinstance(layout, Layout):
            raise RouteConfigError(
                f"{self.relative(file)} must define `layout = Layout(...)` "
                "(from litekit import Layout)"
            )
        return layout.options

    def check_conflicts(self) -> None:
        """Reject routes Litestar would silently drop when sibling routers overlap."""
        defined: dict[tuple[tuple[str, ...], str], Path] = {}
        params: dict[tuple[str, ...], tuple[str, Path]] = {}
        for endpoint in self.endpoints:
            other = defined.setdefault((endpoint.segments, endpoint.method), endpoint.source)
            if other != endpoint.source:
                raise RouteConfigError(
                    f"{endpoint.method} {_display(endpoint.segments)} is defined in both "
                    f"{self.relative(other)} and {self.relative(endpoint.source)}"
                )
            for index, segment in enumerate(endpoint.segments):
                if not _is_param(segment):
                    continue
                prefix = tuple("{}" if _is_param(s) else s for s in endpoint.segments[:index])
                seen, source = params.setdefault(prefix, (segment, endpoint.source))
                if seen != segment:
                    raise RouteConfigError(
                        f"Conflicting path parameters at {_display(prefix + ('*',))}: "
                        f"{seen} in {self.relative(source)} and "
                        f"{segment} in {self.relative(endpoint.source)}. "
                        "Litestar cannot route two different parameters at the same position"
                    )

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def _subdirectories(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.is_dir() and not is_ignored(p.name)]


def _server_handlers(module: ModuleType, source: str) -> list[Any]:
    handlers: list[Any] = []
    for name, value in vars(module).items():
        if isinstance(value, BaseRouteHandler) or _is_own_controller(value, module):
            handlers.append(value)
        elif name in HTTP_METHODS and callable(value):
            handlers.append(HTTPRouteHandler(http_method=HttpMethod(name))(value))
        elif (
            inspect.isfunction(value)
            and name.upper() in HTTP_METHODS
            and value.__module__ == module.__name__
        ):
            raise RouteConfigError(
                f"{source}: `{name}` is not registered; "
                f"HTTP method handlers must be uppercase (`{name.upper()}`)"
            )
    if not handlers:
        warnings.warn(f"{source} defines no route handlers", LiteKitWarning, stacklevel=2)
    return handlers


def _is_own_controller(value: Any, module: ModuleType) -> bool:
    # Only controllers defined here: an imported base class must not become a route.
    return (
        inspect.isclass(value)
        and issubclass(value, Controller)
        and value is not Controller
        and value.__module__ == module.__name__
    )


def _endpoints(handler: Any) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield (path segments below the directory, method) for each endpoint ``handler`` adds."""
    if inspect.isclass(handler) and issubclass(handler, Controller):
        base = _split(getattr(handler, "path", "/"))
        for _, member in inspect.getmembers(handler, lambda v: isinstance(v, BaseRouteHandler)):
            for extra, method in _endpoints(member):
                yield base + extra, method
        return
    methods = getattr(handler, "http_methods", None) or {type(handler).__name__}
    for handler_path in handler.paths:
        for method in methods:
            yield _split(handler_path), method


def _split(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def _is_param(segment: str) -> bool:
    return segment.startswith("{")


def _display(segments: tuple[str, ...]) -> str:
    return "/" + "/".join(segments)


def _module_name(directory_name: str) -> str:
    if directory_name.isidentifier():
        return directory_name
    # ``[id]`` and ``(id)`` both clean up to ``_id_``; the digest keeps them apart.
    cleaned = re.sub(r"\W", "_", directory_name)
    return f"{cleaned}_{hashlib.sha1(directory_name.encode()).hexdigest()[:6]}"


def _ensure_package(name: str, directory: Path) -> None:
    """Register ``directory`` as package ``name`` so route files can use relative imports."""
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


def _load(file: Path, package: str) -> ModuleType:
    # Imported once per process, like any other module: repeated file_router() calls reuse it.
    name = f"{package}.{file.stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise RouteConfigError(f"Cannot import {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
