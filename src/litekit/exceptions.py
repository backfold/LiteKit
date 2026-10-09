from __future__ import annotations

from litestar.exceptions import ImproperlyConfiguredException


class RouteConfigError(ImproperlyConfiguredException):
    """Raised when the routes directory cannot be turned into a valid route tree."""


class LiteKitWarning(UserWarning):
    """Warns about a routes directory that works but is probably not what was meant."""
