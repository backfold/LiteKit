from __future__ import annotations

import difflib
import inspect
from typing import TYPE_CHECKING, Any, Mapping, Sequence, TypedDict

from litestar import Router

if TYPE_CHECKING:
    from litestar import Request, Response, WebSocket
    from litestar.datastructures import CacheControlHeader, ETag
    from litestar.dto import AbstractDTO
    from litestar.openapi.spec import SecurityRequirement
    from litestar.types import (
        AfterRequestHookHandler,
        AfterResponseHookHandler,
        BeforeRequestHookHandler,
        Dependencies,
        ExceptionHandlersMap,
        Guard,
        Middleware,
        ParametersMap,
        ResponseCookies,
        ResponseHeaders,
        TypeDecodersSequence,
        TypeEncodersMap,
    )
    from typing_extensions import Unpack

# The Router arguments this Litestar version accepts; ``Layout`` checks against these at runtime.
ROUTER_OPTIONS = frozenset(inspect.signature(Router.__init__).parameters) - {
    "self",
    "path",
    "route_handlers",
}


class LayoutOptions(TypedDict, total=False):
    """Keyword arguments of :class:`litestar.Router`, minus ``path`` and ``route_handlers``."""

    after_request: AfterRequestHookHandler | None
    after_response: AfterResponseHookHandler | None
    before_request: BeforeRequestHookHandler | None
    cache_control: CacheControlHeader | None
    dependencies: Dependencies | None
    dto: type[AbstractDTO[Any]] | None
    etag: ETag | None
    exception_handlers: ExceptionHandlersMap | None
    guards: Sequence[Guard] | None
    include_in_schema: bool
    middleware: Sequence[Middleware] | None
    opt: Mapping[str, Any] | None
    parameters: ParametersMap | None
    request_class: type[Request[Any, Any, Any]] | None
    request_max_body_size: int | None
    response_class: type[Response[Any]] | None
    response_cookies: ResponseCookies | None
    response_headers: ResponseHeaders | None
    return_dto: type[AbstractDTO[Any]] | None
    security: Sequence[SecurityRequirement] | None
    signature_namespace: Mapping[str, Any] | None
    signature_types: Sequence[Any] | None
    tags: Sequence[str] | None
    type_decoders: TypeDecodersSequence | None
    type_encoders: TypeEncodersMap | None
    websocket_class: type[WebSocket[Any, Any, Any]] | None


class Layout:
    """Router options for a directory and everything below it.

    Define one in ``+layout.py`` as ``layout = Layout(...)``. It takes the same keyword
    arguments as :class:`litestar.Router`, and a misspelled option fails immediately
    instead of being silently ignored.
    """

    __slots__ = ("options",)

    def __init__(self, **options: Unpack[LayoutOptions]) -> None:
        for name in sorted(set(options) - ROUTER_OPTIONS):
            hint = difflib.get_close_matches(name, ROUTER_OPTIONS, n=1)
            suggestion = f"; did you mean {hint[0]!r}?" if hint else ""
            raise TypeError(f"Layout() got an unknown option {name!r}{suggestion}")
        self.options: dict[str, Any] = dict(options)

    def __repr__(self) -> str:
        args = ", ".join(f"{name}={value!r}" for name, value in self.options.items())
        return f"Layout({args})"
