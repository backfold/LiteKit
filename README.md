# LiteKit

SvelteKit-style file-based routing for [Litestar](https://litestar.dev).

```
pip install litekit
```

Requires Python 3.10+ and Litestar 2.13 or newer (but not Litestar 3). The examples use `FromPath` / `FromQuery`, which need Litestar 2.22+; see [Path parameters](#path-parameters) for older versions.

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

`file_router` returns a plain Litestar `Router`, so it mixes freely with ordinary handlers, controllers and other routers.

- A relative directory is resolved from the file that calls `file_router`, not from the working directory, so `litestar run`, `uvicorn app:app` and pytest all find the same folder.
- `path="/api"` mounts the whole tree under a prefix. The prefix may contain path parameters: `file_router("routes", path="/{tenant:str}")`.
- Mounting the tree inside your own `Router(path="/{tenant:str}", ...)` works too. LiteKit cannot see that outer path, so a handler that reads `tenant` triggers a `LiteKitWarning` (see [Path parameters](#path-parameters)). Use `path=` instead, or silence that one warning.
- `sync_to_thread=True` / `False` applies to synchronous method functions (`def GET`); see [`+server.py`](#serverpy).
- `litestar routes` prints the route table that was built.

### `+server.py`

Functions named after an HTTP method (uppercase: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, `TRACE`) become handlers:

```python
# routes/users/[id=int]/+server.py
from litestar.params import FromPath

from .helpers import load_user


async def GET(id: FromPath[int]) -> dict:
    return await load_user(id)


async def DELETE(id: FromPath[int]) -> None:
    ...
```

They become Litestar handlers with default options, so Litestar's defaults apply:

- `POST` answers `201 Created`; `DELETE` answers `204 No Content` and must return `None`.
- `HEAD` is not derived from `GET`: define `HEAD` (returning `None`) if you need it. `OPTIONS` is answered automatically.
- A plain `def` (not `async def`) is handled exactly like Litestar's `@get()` on a `def`: it runs on the event loop thread, and Litestar warns at startup that it could block. Choose explicitly for the whole tree with `file_router("routes", sync_to_thread=True)` (worker thread, for blocking I/O) or `sync_to_thread=False` (stay on the event loop, no warning). `True` breaks objects that must stay on one thread, such as a `sqlite3` connection created at import time. For a single handler use `@get(sync_to_thread=...)`.

For any other option (`status_code`, `name`, `cache`, `sync_to_thread=False`, ...) use a Litestar decorator instead. Every route handler in the module, and every `Controller` and class-based `WebsocketListener` defined in it, is registered too, and their paths are relative to the directory:

```python
# routes/users/[id=int]/+server.py
from litestar import Controller, get, post, websocket_listener
from litestar.handlers import WebsocketListener


@post(status_code=202)              # → POST /users/{id}
async def create(data: dict) -> dict:
    return data


@websocket_listener("/ws")          # → /users/{id}/ws
async def events(data: str) -> str:
    return data


class Chat(WebsocketListener):      # → /users/{id}/chat
    path = "/chat"

    async def on_receive(self, data: str) -> str:
        return data


class Stats(Controller):
    path = "/stats"                 # → /users/{id}/stats

    @get()
    async def stats(self) -> dict:
        return {}
```

Do not repeat the directory in the decorator: `@get("/users")` in `routes/users/+server.py` is served at `/users/users`.

A `Router` object in `+server.py` is an error: every directory already is a Router. Use a subdirectory (with a `+layout.py` for its options), or register your own `Router` in the app next to `file_router(...)`. A Router you only import to use inside the file is fine under a private name (`from myapp.routers import api as _api`), and so is the `Litestar` app object.

The special file names are lowercase and case-sensitive on every OS: `+Server.py` is an error even on macOS and Windows, where the file system would otherwise load it locally while a Linux server ignores it.

Route files are ordinary modules: relative imports work (`from .helpers import x`, `from ..shared import y`), and each file is imported once per process. If `routes/` lives inside your package, import shared code from outside `routes/` (for example `from myapp.db import ...`) rather than importing a route folder's helper by its absolute name, or that helper is loaded twice.

#### Path parameters

The directory name is the parameter name. Declare it with `FromPath`, and query parameters with `FromQuery`:

```python
# routes/users/[id=int]/+server.py
from litestar.params import FromPath, FromQuery

async def GET(id: FromPath[int], page: FromQuery[int] = 1) -> dict: ...   # /users/3?page=2
# async def GET(user_id: FromPath[int])  → LiteKitWarning at startup: the directory says `id`,
#                                          so Litestar would answer every request with 400
```

On Litestar 2.13-2.21, which have no `FromPath`, write `id: int` with the directory's name; LiteKit then skips the parameter name check below.

Litestar 2.22+ still accepts bare annotations (`id: int`, `page: int`) but deprecates them for both path and query parameters: each one emits a `LitestarDeprecationWarning`, which fails a test run that uses `-W error`. A bare `user_id: int` whose name does not match the directory is silently read as a query parameter, so neither LiteKit nor Litestar can tell you meant the path; `FromPath` makes that mistake visible. A `[...path]` value starts with a slash: `/files/a/b.txt` gives `"/a/b.txt"`.

The check understands Litestar's other forms: one handler on several paths with a default (`@get(["/", "/{page:int}"])` and `page: FromPath[int] = 1`), and renamed parameters (`Annotated[int, PathParameter(name="id")]`). It is a warning, not an error, because the parameter may come from an outer `Router` that LiteKit cannot see. If that is your setup, silence it for that parameter only:

```python
import warnings
from litekit import LiteKitWarning

warnings.filterwarnings("ignore", message=r".*declares path parameter 'tenant'", category=LiteKitWarning)
```

#### Models and OpenAPI names

A model's class name becomes its OpenAPI component name (`Item`). If two route files each define their own `Item`, Litestar, like in any app, tells them apart by module path, and you get long names such as `a___server___Item`, which also become class names in generated API clients. Keep models in a shared module outside the route files, for example `routes/_models.py` imported with `from .._models import Item`, or give each one a distinct name (`UserItem`, `PostItem`).

#### Linting

Ruff's `pep8-naming` rules flag the uppercase functions. Silence them for route files only:

```toml
[tool.ruff.lint.per-file-ignores]
"**/+server.py" = ["N802"]
```

### `+layout.py`

A layout is **router configuration**, not an HTML layout: `layout = Layout(...)` takes the same options as [`Router`](https://docs.litestar.dev/latest/reference/router.html) and applies them to the directory and everything below it: `guards`, `dependencies`, `middleware`, `exception_handlers`, `before_request`, `after_request`, `tags`, `opt`, and so on.

```python
# routes/(admin)/+layout.py
from litestar.exceptions import NotAuthorizedException

from litekit import Layout


def require_admin(connection, _) -> None:
    # connection.user is set by your authentication middleware (e.g. Litestar's JWT auth).
    if not connection.user.is_admin:
        raise NotAuthorizedException()


layout = Layout(guards=[require_admin], tags=["admin"])
```

To reuse a set of options, type it with `LayoutOptions`, the `TypedDict` of the keywords `Layout` accepts; you never need it otherwise:

```python
# routes/_options.py
from litekit import LayoutOptions

COMMON: LayoutOptions = {"tags": ["api"], "response_headers": {"x-app": "demo"}}

# routes/(admin)/+layout.py:  layout = Layout(**COMMON, guards=[require_admin])
```

A misspelled option raises right away (`Layout(guard=...)` → `did you mean 'guards'?`) instead of silently leaving routes unprotected. A layout in a group such as `(admin)` applies only to the routes inside that group.

### Directory names

| Directory      | Path segment     |
| -------------- | ---------------- |
| `users`        | `users`          |
| `[id]`         | `{id:str}`       |
| `[id=int]`     | `{id:int}`, and also `float`, `decimal`, `uuid`, `date`, `datetime`, `time`, `timedelta`, `path` |
| `[...rest]`    | `{rest:path}`. It must be a leaf, and it does not match an empty path: add a `+server.py` in the parent for that. |
| `(group)`      | nothing. Groups routes so they can share a `+layout.py`. |
| `_private`, `.hidden` | ignored. Use them for shared code. |

Unlike SvelteKit's custom matchers, `[id=int]` uses Litestar's built-in path parameter types. Optional parameters (`[[lang]]`) are not supported, and Litestar's own `{id:int}` syntax is rejected as a directory name (it is not a valid file name on Windows).

### Errors instead of surprises

`file_router` refuses route trees that Litestar would accept but serve differently than written, and raises `RouteConfigError` (a Litestar `ImproperlyConfiguredException`) at startup:

- the same method and path defined twice, in one file (`GET` next to `@get()`, two controllers, two controllers that inherit the same `@get()`, or two websocket handlers) or in two files (for example through two groups),
- two different parameters at the same position (`[id]` next to `[slug]`, or next to `[...rest]`): Litestar can only route one of them,
- a custom `OPTIONS` handler that Litestar would replace with its automatic one. Litestar keeps it only if it is registered before the other methods of its path; LiteKit registers OPTIONS handlers first within a file, but across files (sorted by name) and inside a `Controller` (members in name order) the order is not obvious. LiteKit checks the route table Litestar actually built, so it only refuses a handler that really is lost and never one Litestar serves. The safe place is a module-level `OPTIONS` function in the same file as the other methods of that path,
- a `Router` object under a public name in `+server.py` (see above),
- `+Server.py`, `+Layout.py` or another case variant of the special file names,
- a lowercase `def get()` in `+server.py` with no `GET` next to it, which would otherwise be ignored (rename helpers to `_get`),
- a `+layout.py` without `layout = Layout(...)`, even in a folder that has no routes yet,
- malformed directory names (`[id`, `[id=bogus]`, `[[lang]]`, `{id:int}`).

It warns with `LiteKitWarning` when a handler declares a `FromPath` parameter (without a default) that none of its paths provide, when a `+server.py` defines no handlers, when the tree has no routes at all, and about unknown `+` files such as a misspelled `+sever.py` or a SvelteKit `+page.py`.

A base controller that should not be served itself belongs in a module outside the route file (for example `_shared.py`, imported with `from ._shared import Base`): only controllers and listeners defined in `+server.py` are registered. If several subclasses in one directory inherit the same handlers, give each one its own `path`.

Conflicts between the file tree and handlers you register elsewhere in the app are not checked.

## Development

```
python -m pip install -e . pytest httpx
python -m pytest
```

Warnings are errors in the test suite, so a new Litestar deprecation shows up in CI first.

## Releasing

1. Bump `version` in `pyproject.toml` and merge it to `main`.
2. Tag the merge commit with the same version and push the tag: `git tag v0.1.0 && git push origin v0.1.0`.

The publish workflow runs the tests, checks that the tag matches `pyproject.toml`, uploads to PyPI and creates the GitHub Release. To re-run it by hand ("Run workflow"), pick the tag, not a branch: a run from a branch fails before anything is uploaded.
