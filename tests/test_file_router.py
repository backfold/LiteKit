from __future__ import annotations

import re
import textwrap
import warnings
from pathlib import Path

import litestar.params
import pytest
from litestar import Litestar, Router
from litestar.exceptions import LitestarWarning
from litestar.testing import TestClient

from litekit import Layout, LiteKitWarning, RouteConfigError, file_router

# FromPath, FromQuery and PathParameter exist since Litestar 2.22. The CI job for the lowest
# supported Litestar runs every other test.
needs_from_path = pytest.mark.skipif(
    not hasattr(litestar.params, "FromPath"), reason="FromPath needs Litestar 2.22+"
)


def write(root: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(textwrap.dedent(source))
    return root


def client(root: Path) -> TestClient:
    return TestClient(Litestar(route_handlers=[file_router(root)]))


def returns(value: str) -> str:
    return f"async def GET() -> str:\n    return {value!r}\n"


def test_static_and_nested_routes(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": returns("home"),
            "users/+server.py": """
                async def GET() -> list[str]:
                    return ["a", "b"]

                async def POST(data: dict) -> dict:
                    return data
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/").text == "home"
        assert c.get("/users").json() == ["a", "b"]
        response = c.post("/users", json={"name": "a"})
        assert response.status_code == 201
        assert response.json() == {"name": "a"}
        assert c.delete("/users").status_code == 405


@needs_from_path
def test_params(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                from litestar.params import FromPath

                async def GET(id: FromPath[int]) -> dict:
                    return {"id": id}
            """,
            "users/[id=int]/posts/+server.py": """
                from litestar.params import FromPath

                async def GET(id: FromPath[int]) -> str:
                    return f"posts of {id}"
            """,
            "posts/[slug]/+server.py": """
                from litestar.params import FromPath

                async def GET(slug: FromPath[str]) -> str:
                    return slug
            """,
            "files/[...path]/+server.py": """
                from pathlib import Path
                from litestar.params import FromPath

                async def GET(path: FromPath[Path]) -> str:
                    return str(path)
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/42").json() == {"id": 42}
        assert c.get("/users/42/posts").text == "posts of 42"
        assert c.get("/users/abc").status_code == 404
        assert c.get("/posts/hello").text == "hello"
        assert c.get("/files/a/b/c.txt").text == "/a/b/c.txt"


@pytest.mark.skipif(
    hasattr(litestar.params, "FromPath"), reason="bare path annotations are deprecated in 2.22+"
)
def test_params_without_from_path(tmp_path: Path) -> None:
    # Litestar < 2.22 has no FromPath: path parameters are matched by name.
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                async def GET(id: int) -> dict:
                    return {"id": id}
            """,
            "files/[...path]/+server.py": """
                from pathlib import Path

                async def GET(path: Path) -> str:
                    return str(path)
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/42").json() == {"id": 42}
        assert c.get("/files/a/b.txt").text == "/a/b.txt"


def test_groups_do_not_add_path_segment(tmp_path: Path) -> None:
    write(tmp_path, {"(marketing)/about/+server.py": returns("about")})
    with client(tmp_path) as c:
        assert c.get("/about").text == "about"


def test_decorated_handlers_and_controllers(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "items/_base.py": """
                from litestar import Controller

                class BaseController(Controller):
                    tags = ["base"]
            """,
            "items/+server.py": """
                from litestar import get, post, websocket_listener

                from ._base import BaseController

                @post(status_code=202)
                async def create(data: dict) -> dict:
                    return data

                @websocket_listener("/ws")
                async def echo(data: str) -> str:
                    return data

                class Stats(BaseController):
                    path = "/stats"

                    @get()
                    async def stats(self) -> dict:
                        return {"count": 1}
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.post("/items", json={}).status_code == 202
        assert c.get("/items/stats").json() == {"count": 1}
        with c.websocket_connect("/items/ws") as ws:
            ws.send_text("hi")
            assert ws.receive_text() == "hi"


@needs_from_path
def test_relative_imports(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_lib/db.py": "NAME = 'db'\n",
            "users/[id]/helpers.py": "PREFIX = 'user'\n",
            "users/[id]/+server.py": """
                from litestar.params import FromPath

                from ..._lib.db import NAME
                from .helpers import PREFIX

                async def GET(id: FromPath[str]) -> str:
                    return f"{PREFIX}:{id}:{NAME}"
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/7").text == "user:7:db"


def test_layout_applies_to_subtree(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": returns("public"),
            "admin/+layout.py": """
                from litestar.exceptions import NotAuthorizedException

                from litekit import Layout

                def require_token(connection, _) -> None:
                    if connection.headers.get("x-token") != "secret":
                        raise NotAuthorizedException()

                layout = Layout(guards=[require_token], response_headers={"x-area": "admin"})
            """,
            "admin/users/+server.py": returns("users"),
        },
    )
    with client(tmp_path) as c:
        assert c.get("/").text == "public"
        assert c.get("/admin/users").status_code == 401
        response = c.get("/admin/users", headers={"x-token": "secret"})
        assert response.text == "users"
        assert response.headers["x-area"] == "admin"


def test_layout_rejects_unknown_option() -> None:
    with pytest.raises(TypeError, match="unknown option 'guard'; did you mean 'guards'"):
        Layout(guard=[])  # type: ignore[call-arg]


def test_layout_file_must_define_layout(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "admin/+layout.py": "guards = []\n",
            "admin/+server.py": returns("admin"),
        },
    )
    with pytest.raises(RouteConfigError, match=r"admin/\+layout.py must define `layout = Layout"):
        file_router(tmp_path)


def test_lowercase_method_is_an_error(tmp_path: Path) -> None:
    write(tmp_path, {"a/+server.py": "async def get() -> str:\n    return ''\n"})
    with pytest.raises(RouteConfigError, match=r"a/\+server.py: `get` is not registered.*`GET`"):
        file_router(tmp_path)


def test_empty_server_file_warns(tmp_path: Path) -> None:
    write(tmp_path, {"a/+server.py": "VALUE = 1\n", "b/+server.py": returns("b")})
    with pytest.warns(LiteKitWarning, match="defines no route handlers"):
        file_router(tmp_path)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {"(a)/x/+server.py": returns("a"), "(b)/x/+server.py": returns("b")},
            r"GET /x is defined in both \(a\)/x/\+server.py and \(b\)/x/\+server.py",
        ),
        (
            {"+server.py": returns("root"), "(g)/+server.py": returns("group")},
            r"GET / is defined in both",
        ),
        (
            {"u/[id=int]/+server.py": returns("int"), "u/[name]/+server.py": returns("name")},
            r"Conflicting path parameters at /u/\*: \{id:int\}.* and \{name:str\}",
        ),
        (
            {"[id]/+server.py": returns("id"), "[...rest]/+server.py": returns("rest")},
            r"Conflicting path parameters at /\*",
        ),
        (
            {"(a)/[id]/+server.py": returns("a"), "(b)/[slug]/x/+server.py": returns("b")},
            r"Conflicting path parameters",
        ),
    ],
)
def test_conflicts_are_errors(tmp_path: Path, files: dict[str, str], message: str) -> None:
    write(tmp_path, files)
    with pytest.raises(RouteConfigError, match=message):
        file_router(tmp_path)


def test_same_path_different_methods_in_different_files(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "(read)/x/+server.py": returns("read"),
            "(write)/x/+server.py": "async def POST() -> str:\n    return 'write'\n",
        },
    )
    file_router(tmp_path)


def test_route_modules_are_imported_once(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "counter.txt": "",
            "+server.py": """
                from pathlib import Path

                counter = Path(__file__).with_name("counter.txt")
                counter.write_text(counter.read_text() + "x")

                async def GET() -> str:
                    return "ok"
            """,
        },
    )
    file_router(tmp_path)
    file_router(tmp_path)
    assert (tmp_path / "counter.txt").read_text() == "x"


def test_ignores_private_dirs(tmp_path: Path) -> None:
    write(tmp_path, {"_lib/+server.py": returns("hidden"), "api/+server.py": returns("api")})
    with client(tmp_path) as c:
        assert c.get("/_lib").status_code == 404
        assert c.get("/api").text == "api"


def test_mount_path(tmp_path: Path) -> None:
    write(tmp_path, {"ping/+server.py": returns("pong")})
    with TestClient(Litestar(route_handlers=[file_router(tmp_path, path="/api")])) as c:
        assert c.get("/api/ping").text == "pong"


@pytest.mark.parametrize(
    ("directory", "message"),
    [
        ("[id=bogus]", "Unknown parameter type"),
        ("[id", "Malformed route segment"),
    ],
)
def test_invalid_segments(tmp_path: Path, directory: str, message: str) -> None:
    write(tmp_path, {f"{directory}/+server.py": returns("")})
    with pytest.raises(RouteConfigError, match=message):
        file_router(tmp_path)


def test_rest_param_cannot_have_children(tmp_path: Path) -> None:
    write(tmp_path, {"[...rest]/more/+server.py": returns("")})
    with pytest.raises(RouteConfigError, match="Rest parameter"):
        file_router(tmp_path)


def test_relative_directory_resolves_from_caller() -> None:
    with TestClient(Litestar(route_handlers=[file_router("fixtures/routes")])) as c:
        assert c.get("/hello").text == "hello"


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RouteConfigError, match="not found"):
        file_router(tmp_path / "nope")


def test_controller_without_path(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "items/+server.py": """
                from litestar import Controller, get

                class Items(Controller):
                    @get()
                    async def index(self) -> str:
                        return "items"
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/items").text == "items"


@pytest.mark.parametrize(
    "source",
    [
        """
        from litestar import get

        @get()
        async def index() -> str:
            return "decorated"

        async def GET() -> str:
            return "function"
        """,
        """
        from litestar import Controller, get

        class A(Controller):
            @get()
            async def a(self) -> str:
                return "a"

        class B(Controller):
            @get()
            async def b(self) -> str:
                return "b"
        """,
    ],
    ids=["function-and-decorator", "two-controllers"],
)
def test_duplicate_in_one_file_is_an_error(tmp_path: Path, source: str) -> None:
    write(tmp_path, {"+server.py": source})
    with pytest.raises(RouteConfigError, match=r"GET / is defined twice in \+server.py"):
        file_router(tmp_path)


@needs_from_path
def test_path_parameter_not_in_directory_warns(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                from litestar.params import FromPath

                async def GET(user_id: FromPath[int]) -> int:
                    return user_id
            """,
        },
    )
    with pytest.warns(
        LiteKitWarning,
        match=r"`GET` declares path parameter 'user_id', but /users/\{id:int\} only has: id",
    ):
        file_router(tmp_path)


@needs_from_path
def test_path_parameter_from_handler_path(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                from litestar import get
                from litestar.params import FromPath

                @get("/posts/{post:int}")
                async def post(id: FromPath[int], post: FromPath[int]) -> list[int]:
                    return [id, post]
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/1/posts/2").json() == [1, 2]


@needs_from_path
def test_path_parameter_from_mount_path(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/+server.py": """
                from litestar.params import FromPath

                async def GET(tenant: FromPath[str]) -> str:
                    return tenant
            """,
        },
    )
    router = file_router(tmp_path, path="/{tenant:str}")
    with TestClient(Litestar(route_handlers=[router])) as c:
        assert c.get("/acme/users").text == "acme"


THREAD_IDS = """
import threading

def GET() -> int:
    return threading.get_ident()

async def PUT() -> int:
    return threading.get_ident()
"""


def test_sync_function_keeps_litestar_default(tmp_path: Path) -> None:
    # Same as a plain `@get()` on a `def`: Litestar warns and runs it on the event loop thread,
    # so thread-bound objects (sqlite connections, thread locals) keep working.
    write(tmp_path, {"+server.py": THREAD_IDS})
    with pytest.warns(LitestarWarning, match="without setting sync_to_thread"):
        app = Litestar(route_handlers=[file_router(tmp_path)])
    with TestClient(app) as c:
        assert c.get("/").json() == c.put("/").json()


@pytest.mark.parametrize("sync_to_thread", [False, True])
def test_sync_to_thread_option(tmp_path: Path, sync_to_thread: bool) -> None:
    write(tmp_path, {"+server.py": THREAD_IDS})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        app = Litestar(route_handlers=[file_router(tmp_path, sync_to_thread=sync_to_thread)])
    with TestClient(app) as c:
        in_worker_thread = c.get("/").json() != c.put("/").json()
    assert in_worker_thread is sync_to_thread


def test_custom_options_handler_is_kept(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Response

                async def GET() -> str:
                    return "get"

                async def OPTIONS() -> Response[None]:
                    return Response(None, headers={"allow": "GET, OPTIONS"}, status_code=200)
            """,
        },
    )
    with client(tmp_path) as c:
        response = c.options("/")
        assert response.status_code == 200
        assert response.headers["allow"] == "GET, OPTIONS"


def test_options_split_across_files_is_an_error_when_lost(tmp_path: Path) -> None:
    # (a) is registered before (b), so Litestar has already added its own OPTIONS for /x.
    write(
        tmp_path,
        {
            "(a)/x/+server.py": returns("get"),
            "(b)/x/+server.py": "async def OPTIONS() -> None:\n    return None\n",
        },
    )
    with pytest.raises(
        RouteConfigError,
        match=r"Litestar would replace `OPTIONS` \(OPTIONS /x in \(b\)/x/\+server.py\).*"
        r"same file as the other methods for that path \(\(a\)/x/\+server.py\)",
    ):
        file_router(tmp_path)


def test_options_in_controller_after_other_method_is_an_error(tmp_path: Path) -> None:
    # Litestar registers controller members in name order: `index` before `options`.
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, HttpMethod, get, route

                class Items(Controller):
                    @get()
                    async def index(self) -> str:
                        return "get"

                    @route(http_method=HttpMethod.OPTIONS)
                    async def options(self) -> None:
                        return None
            """,
        },
    )
    with pytest.raises(
        RouteConfigError,
        match=r"Litestar would replace `Items.options`.*move it out of Controller `Items`",
    ):
        file_router(tmp_path)


OPTIONS_RESPONSE = """
from litestar import Response

async def OPTIONS() -> Response[None]:
    return Response(None, headers={"allow": "custom"}, status_code=200)
"""


@pytest.mark.parametrize(
    "files",
    [
        # OPTIONS in a file that is registered before the other methods of its path.
        {"(a)/x/+server.py": OPTIONS_RESPONSE, "(b)/x/+server.py": returns("get")},
        # A Controller whose OPTIONS member sorts before its other member (`options` < `show`).
        {
            "x/+server.py": """
                from litestar import Controller, HttpMethod, Response, get, route

                class Items(Controller):
                    @route(http_method=HttpMethod.OPTIONS)
                    async def options(self) -> Response[None]:
                        return Response(None, headers={"allow": "custom"}, status_code=200)

                    @get()
                    async def show(self) -> str:
                        return "get"
            """
        },
    ],
    ids=["other-file-registered-later", "controller-member-order"],
)
def test_options_that_litestar_keeps_is_accepted(tmp_path: Path, files: dict[str, str]) -> None:
    write(tmp_path, files)
    with client(tmp_path) as c:
        assert c.get("/x").text == "get"
        response = c.options("/x")
        assert response.status_code == 200
        assert response.headers["allow"] == "custom"


def test_options_function_next_to_controller_is_kept(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, Response, get

                class Items(Controller):
                    @get()
                    async def index(self) -> str:
                        return "get"

                async def OPTIONS() -> Response[None]:
                    return Response(None, headers={"allow": "GET, OPTIONS"}, status_code=200)
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.options("/").headers["allow"] == "GET, OPTIONS"


def test_lowercase_helper_next_to_handler_is_allowed(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                def delete(store: dict) -> None:
                    store.clear()

                async def DELETE() -> None:
                    delete({})
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.delete("/").status_code == 204


def test_unknown_plus_file_warns(tmp_path: Path) -> None:
    write(tmp_path, {"+server.py": returns("ok"), "a/+page.py": returns("page")})
    with pytest.warns(LiteKitWarning, match=r"a/\+page.py is ignored"):
        file_router(tmp_path)


def test_empty_tree_warns(tmp_path: Path) -> None:
    write(tmp_path, {"README.txt": ""})
    with pytest.warns(LiteKitWarning, match="No routes found"):
        file_router(tmp_path)


def test_layout_is_checked_even_without_routes(tmp_path: Path) -> None:
    write(tmp_path, {"+server.py": returns("ok"), "later/+layout.py": "layout = None\n"})
    with pytest.raises(RouteConfigError, match=r"later/\+layout.py must define"):
        file_router(tmp_path)


def test_group_layout_does_not_leak_to_siblings(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "(admin)/+layout.py": """
                from litestar.exceptions import NotAuthorizedException

                from litekit import Layout

                def deny(connection, _) -> None:
                    raise NotAuthorizedException()

                layout = Layout(guards=[deny])
            """,
            "(admin)/dashboard/+server.py": returns("dashboard"),
            "public/+server.py": returns("public"),
        },
    )
    with client(tmp_path) as c:
        assert c.get("/dashboard").status_code == 401
        assert c.get("/public").text == "public"


def test_openapi_component_names_are_valid(tmp_path: Path) -> None:
    model = """
        from dataclasses import dataclass

        @dataclass
        class Item:
            {field}: int

        async def GET() -> Item:
            return Item(1)
    """
    write(
        tmp_path,
        {"a/+server.py": model.format(field="a"), "b/+server.py": model.format(field="b")},
    )
    with client(tmp_path) as c:
        schemas = c.get("/schema/openapi.json").json()["components"]["schemas"]
    assert len(schemas) == 2
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in schemas)


def test_error_message_has_no_status_code(tmp_path: Path) -> None:
    write(tmp_path, {"[id=bogus]/+server.py": returns("")})
    with pytest.raises(RouteConfigError) as info:
        file_router(tmp_path)
    assert str(info.value).startswith("Unknown parameter type")


@pytest.mark.parametrize(
    ("directory", "message"),
    [
        ("{id:int}", r"uses Litestar path syntax; name it \[name\]"),
        ("[[lang]]", "Optional parameters"),
    ],
)
def test_unsupported_directory_syntax(tmp_path: Path, directory: str, message: str) -> None:
    write(tmp_path, {f"{directory}/+server.py": returns("")})
    with pytest.raises(RouteConfigError, match=message):
        file_router(tmp_path)


@needs_from_path
def test_one_handler_on_several_paths_with_default(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "items/+server.py": """
                from litestar import get
                from litestar.params import FromPath

                @get(["/", "/{page:int}"])
                async def items(page: FromPath[int] = 1) -> int:
                    return page
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/items").text == "1"
        assert c.get("/items/4").text == "4"


@needs_from_path
def test_path_parameter_alias(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                from typing import Annotated

                from litestar.params import PathParameter

                async def GET(user_id: Annotated[int, PathParameter(name="id")]) -> int:
                    return user_id
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/5").text == "5"


@needs_from_path
def test_path_parameter_from_outer_router(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/+server.py": """
                from litestar.params import FromPath

                async def GET(tenant: FromPath[str]) -> str:
                    return tenant
            """,
        },
    )
    # LiteKit cannot see the outer Router, so it warns, but it must not refuse the tree.
    with pytest.warns(LiteKitWarning, match="unless an outer Router provides 'tenant'"):
        router = file_router(tmp_path)
    app = Litestar(route_handlers=[Router(path="/{tenant:str}", route_handlers=[router])])
    with TestClient(app) as c:
        assert c.get("/acme/users").text == "acme"


def test_controllers_sharing_an_inherited_handler(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, get

                class Base(Controller):
                    @get()
                    async def index(self) -> str:
                        return type(self).__name__

                class A(Base):
                    pass
            """,
        },
    )
    with pytest.raises(
        RouteConfigError,
        match=r"GET / is defined twice in \+server.py \(`Base.index` and `A.index`\).*inherit",
    ):
        file_router(tmp_path)


def test_inherited_handler_on_different_paths(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, get

                class Base(Controller):
                    path = "/base"

                    @get()
                    async def index(self) -> str:
                        return type(self).__name__

                class A(Base):
                    path = "/a"
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/base").text == "Base"
        assert c.get("/a").text == "A"


def test_websocket_listener_class(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "chat/+server.py": """
                from litestar.handlers import WebsocketListener

                class Base(WebsocketListener):  # abstract: no on_receive, so not a route
                    pass

                class Chat(Base):
                    path = "/"

                    async def on_receive(self, data: str) -> str:
                        return data.upper()
            """,
        },
    )
    with client(tmp_path) as c, c.websocket_connect("/chat") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "HI"


def test_two_websocket_handlers_on_one_path_is_an_error(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "chat/+server.py": """
                from litestar import websocket_listener
                from litestar.handlers import WebsocketListener

                class Chat(WebsocketListener):
                    async def on_receive(self, data: str) -> str:
                        return data

                @websocket_listener("/")
                async def echo(data: str) -> str:
                    return data
            """,
        },
    )
    with pytest.raises(RouteConfigError, match=r"websocket /chat is defined twice"):
        file_router(tmp_path)


def test_router_in_route_file_is_an_error(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "api/+server.py": """
                from litestar import Router, get

                @get("/x")
                async def x() -> str:
                    return "x"

                v1 = Router(path="/v1", route_handlers=[x])
            """,
        },
    )
    with pytest.raises(RouteConfigError, match=r"api/\+server.py: `v1` is a Router"):
        file_router(tmp_path)


def test_options_only_controller_next_to_get_function(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, HttpMethod, Response, route

                async def GET() -> str:
                    return "get"

                class Preflight(Controller):
                    @route(http_method=HttpMethod.OPTIONS)
                    async def options(self) -> Response[None]:
                        return Response(None, headers={"allow": "custom"}, status_code=200)
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/").text == "get"
        assert c.options("/").headers["allow"] == "custom"


def test_replaced_options_handler_is_an_error(tmp_path: Path) -> None:
    # Each controller has OPTIONS on one path and another method on the other path, so no
    # registration order keeps both; LiteKit checks what Litestar actually built.
    write(
        tmp_path,
        {
            "+server.py": """
                from litestar import Controller, HttpMethod, get, route

                class A(Controller):
                    @route("/", http_method=HttpMethod.OPTIONS)
                    async def options(self) -> None:
                        return None

                    @get("/x")
                    async def x(self) -> str:
                        return "x"

                class B(Controller):
                    @get("/")
                    async def index(self) -> str:
                        return "index"

                    @route("/x", http_method=HttpMethod.OPTIONS)
                    async def options_x(self) -> None:
                        return None
            """,
        },
    )
    with pytest.raises(RouteConfigError, match=r"Litestar would replace `B.options_x`"):
        file_router(tmp_path)


def test_controllers_inheriting_from_a_shared_base(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_shared.py": """
                from litestar import Controller, get

                class Base(Controller):
                    @get()
                    async def index(self) -> str:
                        return type(self).__name__
            """,
            "+server.py": """
                from ._shared import Base

                class A(Base):
                    pass

                class B(Base):
                    pass
            """,
        },
    )
    with pytest.raises(RouteConfigError, match=r"Both inherit it from `Base`") as info:
        file_router(tmp_path)
    assert "out of the route file" not in str(info.value)


@needs_from_path
def test_query_parameters_with_from_query(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/[id=int]/+server.py": """
                from litestar.params import FromPath, FromQuery

                async def GET(id: FromPath[int], page: FromQuery[int] = 1) -> list[int]:
                    return [id, page]
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/users/3").json() == [3, 1]
        assert c.get("/users/3?page=2").json() == [3, 2]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("+Server.py", "+server.py"), ("+Layout.py", "+layout.py"), ("+server.PY", "+server.py")],
)
def test_misspelled_case_is_an_error_on_every_os(tmp_path: Path, name: str, expected: str) -> None:
    # macOS and Windows would load `+Server.py` as `+server.py`, Linux would not: same error.
    write(tmp_path, {"+server.py": returns("ok"), f"a/{name}": returns("a")})
    with pytest.raises(RouteConfigError, match=rf"a/\{name} must be named \{expected}"):
        file_router(tmp_path)


def test_private_or_app_router_in_route_file_is_allowed(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_shared.py": """
                from litestar import Litestar, Router

                app = Litestar(route_handlers=[])
                r = Router(path="/r", route_handlers=[])
            """,
            "+server.py": """
                from ._shared import app
                from ._shared import r as _r

                async def GET() -> str:
                    return f"{len(_r.routes)} {type(app).__name__}"
            """,
        },
    )
    with client(tmp_path) as c:
        assert c.get("/").text == "0 Litestar"


def test_openapi_names_with_shared_models_module(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_models.py": """
                from dataclasses import dataclass

                @dataclass
                class Item:
                    id: int
            """,
            "a/+server.py": """
                from .._models import Item

                async def GET() -> Item:
                    return Item(1)
            """,
            "b/+server.py": """
                from .._models import Item

                async def GET() -> list[Item]:
                    return [Item(2)]
            """,
        },
    )
    with client(tmp_path) as c:
        schemas = c.get("/schema/openapi.json").json()["components"]["schemas"]
    assert list(schemas) == ["Item"]


def test_layout_options_types_a_reusable_dict(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_options.py": """
                from litekit import LayoutOptions

                COMMON: LayoutOptions = {"response_headers": {"x-app": "demo"}}
            """,
            "+layout.py": """
                from litekit import Layout

                from ._options import COMMON

                layout = Layout(**COMMON, tags=["root"])
            """,
            "+server.py": returns("ok"),
        },
    )
    with client(tmp_path) as c:
        assert c.get("/").headers["x-app"] == "demo"
