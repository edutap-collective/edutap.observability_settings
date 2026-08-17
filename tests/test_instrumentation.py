"""What the instrumentation may say about a request."""

from fastapi import FastAPI

from edutap.observability_settings.instrumentation import UNMATCHED, route_template


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/persons/{person_uid}/photos")
    async def photos(person_uid: str) -> dict:
        return {}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {}

    return app


def _scope(path: str) -> dict:
    return {"type": "http", "method": "GET", "path": path, "headers": []}


def test_a_matched_path_resolves_to_its_template():
    assert route_template(_app(), _scope("/persons/ab12@lmu.de/photos")) == (
        "/persons/{person_uid}/photos"
    )


def test_a_static_path_resolves_to_itself():
    assert route_template(_app(), _scope("/healthz")) == "/healthz"


def test_an_unmatched_path_resolves_to_a_constant_and_never_to_itself():
    """The 404 case is the one that leaks if the fallback is the raw path.

    A request to /persons/<identifier>/typo matches no route, so there is no
    template to fall back on -- and the raw path is exactly what must not be
    exported.
    """
    result = route_template(_app(), _scope("/persons/ab12@lmu.de/typo"))
    assert result == UNMATCHED
    assert "ab12" not in result
