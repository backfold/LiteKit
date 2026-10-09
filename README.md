# LiteKit

SvelteKit-style file-based routing for [Litestar](https://litestar.dev).

```
pip install litekit
```

## Usage

```
app.py
routes/
├── +server.py                 → /
├── +layout.py                 → options for every route below
├── users/
│   ├── +server.py             → /users
│   └── [id=int]/
│       ├── +server.py         → /users/{id:int}
│       └── helpers.py         → importable as `from .helpers import ...`
├── posts/[slug]/+server.py    → /posts/{slug:str}
├── files/[...path]/+server.py → /files/{path:path}
└── (admin)/
    ├── +layout.py             → guards etc. for this group only
    └── dashboard/+server.py   → /dashboard
```

```python
# app.py
from litestar import Litestar
from litekit import file_router

app = Litestar(route_handlers=[file_router("routes")])
```

A relative directory is resolved from the file that calls `file_router`, not the working directory. Pass `path="/api"` to mount the tree under a prefix. The result is a plain Litestar `Router`, so it mixes freely with ordinary handlers and controllers.

### `+server.py`

Functions named after an HTTP method (uppercase) become handlers:

```python
# routes/users/[id=int]/+server.py
from litestar.params import FromPath

from .helpers import load_user


async def GET(id: FromPath[int]) -> dict:
    return await load_user(id)


async def DELETE(id: FromPath[int]) -> None:
    ...
```

Litestar route handlers and `Controller` subclasses defined in the module are registered too, so decorator options keep working. Their paths are relative to the directory:

```python
from litestar import Controller, get, post, websocket_listener


@post(status_code=202)
async def create(data: dict) -> dict:
    return data


@websocket_listener("/ws")  # → /users/{id}/ws
async def events(data: str) -> str:
    return data


class Stats(Controller):
    path = "/stats"  # → /users/{id}/stats

    @get()
    async def stats(self) -> dict:
        return {}
```

Route files are ordinary modules: relative imports work (`from .helpers import x`, `from ..shared import y`), and each file is imported once per process.

### `+layout.py`

`layout = Layout(...)` takes the same options as [`Router`](https://docs.litestar.dev/latest/reference/router.html) and applies them to the directory and everything below it: `guards`, `dependencies`, `middleware`, `exception_handlers`, `before_request`, `after_request`, `tags`, `opt`, and so on.

```python
# routes/(admin)/+layout.py
from litestar.exceptions import NotAuthorizedException

from litekit import Layout


def require_admin(connection, _) -> None:
    if not connection.user.is_admin:
        raise NotAuthorizedException()


layout = Layout(guards=[require_admin], tags=["admin"])
```

A misspelled option raises right away (`Layout(guard=...)` → `did you mean 'guards'?`) instead of silently leaving routes unprotected.

### Directory names

| Directory      | Path segment     |
| -------------- | ---------------- |
| `users`        | `users`          |
| `[id]`         | `{id:str}`       |
| `[id=int]`     | `{id:int}`, and also `float`, `decimal`, `uuid`, `date`, `datetime`, `time`, `timedelta`, `path` |
| `[...rest]`    | `{rest:path}`. It must be a leaf, and it does not match an empty path. |
| `(group)`      | nothing. Groups routes so they can share a `+layout.py`. |
| `_private`, `.hidden` | ignored. Use them for shared code. |

Unlike SvelteKit's custom matchers, `[id=int]` uses Litestar's built-in path parameter types.

### Errors instead of surprises

`file_router` refuses route trees that Litestar would quietly get wrong:

- the same method and path defined in two files (for example through two groups),
- two different parameters at the same position (`[id]` next to `[slug]`, or next to `[...rest]`): Litestar can only route one of them,
- a lowercase `def get()` in `+server.py`, which would otherwise be ignored,
- a `+layout.py` without `layout = Layout(...)`.

A `+server.py` that defines no handlers raises a `LiteKitWarning`.
