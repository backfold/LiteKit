from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from litekit import Layout, LiteKitWarning, RouteConfigError, file_router


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
    write(tmp_path, {"a/+server.py": "VALUE = 1\n"})
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
