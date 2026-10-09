from __future__ import annotations

from litestar.exceptions import ImproperlyConfiguredException


class RouteConfigError(ImproperlyConfiguredException):
    """Raised when the routes directory cannot be turned into a valid route tree.

    It subclasses Litestar's ``ImproperlyConfiguredException``, so existing handling of
    Litestar configuration errors also catches it.
    """

    def __str__(self) -> str:
        # Litestar's HTTP exceptions render as "500: <detail>"; a startup error has no status.
        return str(self.detail)


class LiteKitWarning(UserWarning):
    """Warns about a routes directory that works but is probably not what was meant."""
