from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import re
import sys
import typing
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

from litestar import Controller, HttpMethod, Litestar, Router
from litestar.handlers import (
    BaseRouteHandler,
    HTTPRouteHandler,
    WebsocketListener,
    WebsocketRouteHandler,
)
from litestar.types import Empty
from litestar.utils import is_async_callable

from litekit._layout import Layout
from litekit._segments import is_ignored, segment_to_path
from litekit.exceptions import LiteKitWarning, RouteConfigError

SERVER_FILE = "+server.py"
LAYOUT_FILE = "+layout.py"

# Route files are registered under these module names. A ``+`` would leak into Litestar's
# OpenAPI component names (``a_+server_Item``), which the OpenAPI spec does not allow, and a
# plain ``server`` could shadow a helper module of that name in the same directory.
_MODULE_NAMES = {SERVER_FILE: "__server__", LAYOUT_FILE: "__layout__"}

HTTP_METHODS = frozenset(method.value for method in HttpMethod)

# ``FromPath`` / ``PathParameter`` exist since Litestar 2.22. On older versions the path
# parameter name check (``_check_path_parameters``) is skipped; everything else works.
_PATH_PARAMETER: Any = getattr(importlib.import_module("litestar.params"), "PathParameter", None)


def file_router(
    directory: str | Path, *, path: str = "/", sync_to_thread: bool | None = None
) -> Router:
    """Build a Litestar ``Router`` from a SvelteKit-style routes directory.

    A relative ``directory`` is resolved against the file that calls this function,
    so the result does not depend on the working directory the app is started from.
    ``path`` is the URL prefix the whole tree is mounted under.

    ``sync_to_thread`` is passed to Litestar for synchronous method functions
    (``def GET``), exactly like ``@get(sync_to_thread=...)``. The default ``None`` keeps
    Litestar's behaviour: they run on the event loop thread and Litestar warns about it.
    ``True`` runs them in a worker thread, ``False`` keeps them on the event loop without the
    warning. Decorated handlers and ``async def`` functions are never changed.
    """
    root = Path(directory)
    if not root.is_absolute():
        root = _caller_directory() / root
    root = root.resolve()
    if not root.is_dir():
        raise RouteConfigError(f"Routes directory not found: {root}")

    build = _Build(root, sync_to_thread)
    package = "litekit_routes_" + hashlib.sha1(str(root).encode()).hexdigest()[:12]
    router = build.directory(root, package, _split(path), path)
    build.check()
    if router is None:
        _warn(f"No routes found in {root}")
        return Router(path=path, route_handlers=[])
    build.check_options_served(router)
    return router


def _warn(message: str) -> None:
    """Emit a LiteKitWarning attributed to the code that called ``file_router``."""
    package = Path(__file__).parent
    frame: FrameType | None = sys._getframe(1)
    level = 2
    while frame is not None and Path(frame.f_code.co_filename).parent == package:
        frame = frame.f_back
        level += 1
    warnings.warn(message, LiteKitWarning, stacklevel=level)


def _caller_directory() -> Path:
    # Two frames up: _caller_directory -> file_router -> the caller.
    filename = sys._getframe(2).f_code.co_filename
    caller = Path(filename)
    # ``<stdin>``, ``<string>`` and notebook cells have no real file: use the working directory.
    return caller.resolve().parent if caller.is_file() else Path.cwd()


@dataclass
class _Endpoint:
    segments: tuple[str, ...]
    method: str
    source: Path
    handler: Any
    # The Controller class the handler was found on, if any. Controllers that inherit the
    # same ``@get()`` share one handler object, so the handler alone does not identify a route.
    owner: type[Controller] | None = None


@dataclass
class _Build:
    root: Path
    sync_to_thread: bool | None = None
    endpoints: list[_Endpoint] = field(default_factory=list)

    def directory(
        self, directory: Path, package: str, segments: tuple[str, ...], path: str
    ) -> Router | None:
        _ensure_package(package, directory)
        special = self.special_files(directory)
        handlers: list[Any] = []

        if SERVER_FILE in special:
            server = directory / SERVER_FILE
            source = self.relative(server)
            for handler in _server_handlers(_load(server, package), source, self.sync_to_thread):
                handlers.append(handler)
                owner = handler if _is_controller(handler) else None
                paths: dict[int, tuple[Any, list[tuple[str, ...]]]] = {}
                for extra, method, route_handler in _endpoints(handler):
                    endpoint = _Endpoint(segments + extra, method, server, route_handler, owner)
                    self.endpoints.append(endpoint)
                    paths.setdefault(id(route_handler), (route_handler, []))[1].append(
                        endpoint.segments
                    )
                for route_handler, handler_paths in paths.values():
                    _check_path_parameters(route_handler, handler_paths, source)

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

        # Loaded even when the directory has no routes, so a broken +layout.py never goes unnoticed.
        options = self.layout_options(directory, package) if LAYOUT_FILE in special else {}
        if not handlers:
            return None
        return Router(path=path, route_handlers=handlers, **options)

    def special_files(self, directory: Path) -> set[str]:
        """Return the ``+`` files LiteKit reads in ``directory``, checking their exact spelling.

        ``Path.is_file()`` cannot be used for this: on macOS and Windows the file system ignores
        case, so ``+Server.py`` would be found as ``+server.py`` there and then be missing on a
        Linux server. Only the names the directory actually lists count.
        """
        found: set[str] = set()
        for file in sorted(directory.glob("+*")):
            if not file.is_file():
                continue
            if file.name in _MODULE_NAMES:
                found.add(file.name)
            elif file.name.lower() in _MODULE_NAMES:
                expected = file.name.lower()
                raise RouteConfigError(
                    f"{self.relative(file)} must be named {expected} (all lowercase). On macOS "
                    "and Windows it would be loaded anyway, but on Linux (servers, CI, Docker) "
                    "it would be ignored"
                )
            else:
                _warn(
                    f"{self.relative(file)} is ignored: LiteKit only reads "
                    f"{SERVER_FILE} and {LAYOUT_FILE}"
                )
        return found

    def layout_options(self, directory: Path, package: str) -> dict[str, Any]:
        file = directory / LAYOUT_FILE
        layout = getattr(_load(file, package), "layout", None)
        if not isinstance(layout, Layout):
            raise RouteConfigError(
                f"{self.relative(file)} must define `layout = Layout(...)` "
                "(from litekit import Layout)"
            )
        return layout.options

    def check(self) -> None:
        """Reject route trees that Litestar would accept but serve differently than written."""
        defined: dict[tuple[tuple[str, ...], str], _Endpoint] = {}
        params: dict[tuple[str, ...], tuple[str, Path]] = {}
        for endpoint in self.endpoints:
            key = (endpoint.segments, endpoint.method)
            other = defined.setdefault(key, endpoint)
            if (other.owner, other.handler) != (endpoint.owner, endpoint.handler):
                where = (
                    f"twice in {self.relative(endpoint.source)} "
                    f"(`{_qualified(other)}` and `{_qualified(endpoint)}`)"
                    if other.source == endpoint.source
                    else f"in both {self.relative(other.source)} and {self.relative(endpoint.source)}"
                )
                hint = _inherited_hint(other, endpoint) if other.handler is endpoint.handler else ""
                raise RouteConfigError(
                    f"{endpoint.method} {_display(endpoint.segments)} is defined {where}; "
                    f"Litestar would silently serve only one of them{hint}"
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

    def check_options_served(self, router: Router) -> None:
        """Reject a custom OPTIONS handler that Litestar replaced with its automatic one.

        Litestar keeps a custom OPTIONS handler only if it is registered before the other
        methods of its path. Within one file LiteKit registers OPTIONS handlers first; across
        files and inside a Controller the order depends on file and member names. Whether it
        was lost is checked on the route table Litestar actually built, so every order that
        works in Litestar is accepted and only a handler that is really lost is an error.
        """
        served = router.route_handler_method_map
        for endpoint in self.endpoints:
            if endpoint.method != HttpMethod.OPTIONS.value:
                continue
            handler = served.get(_display(endpoint.segments), {}).get(HttpMethod.OPTIONS.value)
            if handler is not None and _function(handler) is _function(endpoint.handler):
                continue
            raise RouteConfigError(
                f"Litestar would replace `{_qualified(endpoint)}` (OPTIONS "
                f"{_display(endpoint.segments)} in {self.relative(endpoint.source)}) with its "
                "automatic OPTIONS handler, because another method for that path is registered "
                f"before it; {self.options_hint(endpoint)}"
            )

    def options_hint(self, endpoint: _Endpoint) -> str:
        """How to keep a lost OPTIONS handler, depending on where the other methods are."""
        others = [
            other
            for other in self.endpoints
            if other.segments == endpoint.segments
            and other.method in HTTP_METHODS
            and other.method != HttpMethod.OPTIONS.value
        ]
        elsewhere = sorted({self.relative(o.source) for o in others if o.source != endpoint.source})
        if elsewhere:
            return (
                "define it as a module-level `OPTIONS` function in the same file as the other "
                f"methods for that path ({', '.join(elsewhere)})"
            )
        if endpoint.owner is not None:
            return (
                f"move it out of Controller `{endpoint.owner.__name__}` into a module-level "
                "`OPTIONS` function in that file"
            )
        return "define it as a module-level `OPTIONS` function in that file instead"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def _subdirectories(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.is_dir() and not is_ignored(p.name)]


def _server_handlers(module: ModuleType, source: str, sync_to_thread: bool | None) -> list[Any]:
    handlers: list[Any] = []
    namespace = vars(module)
    for name, value in namespace.items():
        if isinstance(value, Router) and not _is_private_router(name, value):
            # Litestar would mount it, but LiteKit builds the Routers itself: one per directory.
            raise RouteConfigError(
                f"{source}: `{name}` is a Router, and LiteKit does not mount Routers found in a "
                "route file. Use a subdirectory instead (each directory becomes a Router; put "
                "its options in +layout.py), or register the Router in your app next to "
                f"file_router(...). If `{name}` is only imported for use in this file, give it "
                f"a private name: `import ... as _{name}`"
            )
        if isinstance(value, BaseRouteHandler) or _is_own_route_class(value, module):
            handlers.append(value)
        elif name in HTTP_METHODS and callable(value):
            # Litestar only accepts ``sync_to_thread`` for synchronous callables.
            handlers.append(
                HTTPRouteHandler(
                    http_method=HttpMethod(name),
                    sync_to_thread=None if is_async_callable(value) else sync_to_thread,
                )(value)
            )
        elif (
            inspect.isfunction(value)
            and name.upper() in HTTP_METHODS
            and name.upper() not in namespace
            and value.__module__ == module.__name__
        ):
            raise RouteConfigError(
                f"{source}: `{name}` is not registered; HTTP method handlers must be "
                f"uppercase (`{name.upper()}`). If `{name}` is a helper, rename it (e.g. `_{name}`)"
            )
    if not handlers:
        _warn(f"{source} defines no route handlers")
    # See _Build.check_options_served: a custom OPTIONS handler must be registered first.
    handlers.sort(key=lambda handler: HttpMethod.OPTIONS not in _methods(handler))
    return handlers


def _is_private_router(name: str, value: Router) -> bool:
    """A Router that is clearly not meant as a route: a ``_name`` or the ``Litestar`` app."""
    return name.startswith("_") or isinstance(value, Litestar)


def _methods(handler: Any) -> set[str]:
    return {method for _, method, _ in _endpoints(handler)}


def _is_controller(value: Any) -> bool:
    return inspect.isclass(value) and issubclass(value, Controller)


def _is_listener(value: Any) -> bool:
    return inspect.isclass(value) and issubclass(value, WebsocketListener)


def _is_own_route_class(value: Any, module: ModuleType) -> bool:
    """A Controller or class-based WebsocketListener defined in this route file.

    Only classes defined here: an imported base class must not become a route. An abstract
    listener (no ``on_receive``) is a base class that Litestar could not instantiate.
    """
    if not (_is_controller(value) or _is_listener(value)):
        return False
    return (
        value not in (Controller, WebsocketListener)
        and value.__module__ == module.__name__
        and not inspect.isabstract(value)
    )


def _inherited_hint(first: _Endpoint, second: _Endpoint) -> str:
    """Explain a conflict between two controllers that inherit the same handler."""
    if first.owner is None or second.owner is None:
        return ""
    base = next(
        (
            cls
            for cls in second.owner.__mro__
            if any(value is second.handler for value in vars(cls).values())
        ),
        second.owner,
    )
    if base in (first.owner, second.owner):
        # The base class is defined in this route file, so it is registered as well.
        return (
            f". `{second.owner.__name__}` inherits it from `{base.__name__}`, which is "
            "registered too: give each controller its own `path`, or move "
            f"`{base.__name__}` out of the route file (e.g. into a `_shared.py` module)"
        )
    return (
        f". Both inherit it from `{base.__name__}`: give each controller its own `path`, "
        "or merge them into one"
    )


def _endpoints(handler: Any) -> Iterator[tuple[tuple[str, ...], str, Any]]:
    """Yield (path segments below the directory, method, route handler) for ``handler``."""
    if _is_listener(handler):
        # A class-based listener: ``path`` is a str, a list of str or None ("/").
        listener_paths = handler.path or "/"
        if isinstance(listener_paths, str):
            listener_paths = [listener_paths]
        for listener_path in listener_paths:
            yield _split(listener_path), "websocket", handler
        return
    if _is_controller(handler):
        # A Controller without ``path`` has an unset slot here; Litestar then uses "/".
        path = getattr(handler, "path", None)
        base = _split(path) if isinstance(path, str) else ()
        for _, member in inspect.getmembers(handler, lambda v: isinstance(v, BaseRouteHandler)):
            for extra, method, route_handler in _endpoints(member):
                yield base + extra, method, route_handler
        return
    methods: set[str]
    if isinstance(handler, HTTPRouteHandler):
        methods = set(handler.http_methods)
    else:
        # Litestar keeps one websocket and one ASGI handler per path, like one per method.
        methods = {"websocket" if isinstance(handler, WebsocketRouteHandler) else "asgi"}
    for handler_path in handler.paths:
        for method in sorted(methods):
            yield _split(handler_path), method, handler


def _check_path_parameters(handler: Any, paths: list[tuple[str, ...]], source: str) -> None:
    """Warn at startup instead of answering every request with 400 'Missing path parameter'.

    One handler may serve several paths (``@get(["/", "/{page:int}"])``); a parameter only
    needs to appear in one of them. A parameter with a default is never reported. Only a
    warning: the parameter may legitimately come from an outer ``Router`` that LiteKit cannot
    see, e.g. ``Router(path="/{tenant:str}", route_handlers=[file_router("routes")])``.
    """
    available = {
        segment[1:].split(":")[0]
        for segments in paths
        for segment in segments
        if _is_param(segment)
    }
    for name in _required_path_parameters(handler):
        if name in available:
            continue
        have = ", ".join(sorted(available)) or "none"
        _warn(
            f"{source}: `{_name(handler)}` declares path parameter {name!r}, but "
            f"{' and '.join(_display(p) for p in paths)} only has: {have}, so Litestar will "
            f"answer 400 unless an outer Router provides {name!r}. A path parameter is named "
            f"after its directory, e.g. [{name}] or [{name}=int]"
        )


def _required_path_parameters(handler: Any) -> list[str]:
    """Names of the ``FromPath`` parameters ``handler`` declares without a default."""
    fn: Any = getattr(handler, "fn", None)
    if fn is None or _PATH_PARAMETER is None:
        return []
    while isinstance(fn, functools.partial):
        fn = fn.func
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
        parameters = inspect.signature(fn).parameters
    except (NameError, TypeError, AttributeError, SyntaxError, ValueError):
        # Unresolvable annotations (e.g. names imported under TYPE_CHECKING): Litestar
        # reports or resolves those itself, so skip this check rather than guess.
        return []
    names = []
    for name, hint in hints.items():
        if typing.get_origin(hint) is not typing.Annotated or name not in parameters:
            continue
        for meta in hint.__metadata__:
            if not isinstance(meta, _PATH_PARAMETER):
                continue
            has_default = (
                parameters[name].default is not inspect.Parameter.empty
                or meta.default is not Empty
                or meta.required is False
            )
            if not has_default:
                # ``PathParameter(name="id")`` reads the path parameter ``id``.
                names.append(meta.name or name)
    return names


def _qualified(endpoint: _Endpoint) -> str:
    name = _name(endpoint.handler)
    return f"{endpoint.owner.__name__}.{name}" if endpoint.owner is not None else name


def _name(handler: Any) -> str:
    if inspect.isclass(handler):
        return handler.__name__
    return str(getattr(handler, "handler_name", handler))


def _function(handler: Any) -> Any:
    # Controllers serve a bound copy of each handler, so compare the underlying function.
    fn = getattr(handler, "fn", None)
    return getattr(fn, "__func__", fn)


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
    name = f"{package}.{_MODULE_NAMES[file.name]}"
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
