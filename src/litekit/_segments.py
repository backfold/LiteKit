from __future__ import annotations

import re

from litekit.exceptions import RouteConfigError

PARAM_TYPES = frozenset(
    {"str", "int", "float", "decimal", "uuid", "date", "datetime", "time", "timedelta", "path"}
)

_PARAM = re.compile(r"^\[(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:=(?P<type>[a-z]+))?\]$")
_REST = re.compile(r"^\[\.\.\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\]$")
_GROUP = re.compile(r"^\(.+\)$")


def is_group(segment: str) -> bool:
    return bool(_GROUP.match(segment))


def is_ignored(segment: str) -> bool:
    return segment.startswith((".", "_"))


def segment_to_path(segment: str) -> str:
    """Translate one directory name into a Litestar path segment.

    ``users`` -> ``users``, ``[id]`` -> ``{id:str}``, ``[id=int]`` -> ``{id:int}``,
    ``[...rest]`` -> ``{rest:path}``, ``(group)`` -> ``""``.
    """
    if is_group(segment):
        return ""
    if match := _REST.match(segment):
        return f"{{{match['name']}:path}}"
    if match := _PARAM.match(segment):
        type_ = match["type"] or "str"
        if type_ not in PARAM_TYPES:
            raise RouteConfigError(
                f"Unknown parameter type {type_!r} in {segment!r}; "
                f"expected one of: {', '.join(sorted(PARAM_TYPES))}"
            )
        return f"{{{match['name']}:{type_}}}"
    if "[" in segment or "]" in segment:
        raise RouteConfigError(f"Malformed route segment {segment!r}")
    return segment
