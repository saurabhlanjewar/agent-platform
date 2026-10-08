from fastapi.testclient import TestClient

from platform_app.agents.research.app import create_app
from platform_app.agents.research.config import Settings


def make_app(tmp_path):
    return create_app(
        Settings(
            "test.auth0.com",
            "https://test/api",
            anthropic_key="test",
            tavily_key="test",
            database=str(tmp_path / "test.sqlite3"),
        )
    )


def test_missing_and_malformed_tokens_denied(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for headers in (
            {},
            {"Authorization": "Basic invalid"},
            {"Authorization": "Bearer not-a-jwt"},
        ):
            assert client.get("/research", headers=headers).status_code in (400, 401)
            assert client.post(
                "/research", headers=headers, json={"question": "Research this"}
            ).status_code in (400, 401)


def test_results_are_scoped_by_authenticated_owner(tmp_path):
    app = make_app(tmp_path)
    app.dependency_overrides[app.state.read_auth] = lambda: {"sub": "bob"}
    with TestClient(app) as client:
        saved = app.state.store.save("alice", "Private question", {"status": "completed"})
        assert client.get("/research").json() == {"runs": []}
        assert client.get("/research/" + saved["id"]).status_code == 404
        app.dependency_overrides[app.state.read_auth] = lambda: {"sub": "alice"}
        assert client.get("/research/" + saved["id"]).json()["question"] == "Private question"


def test_execution_is_saved_for_user(tmp_path):
    app = make_app(tmp_path)
    app.dependency_overrides[app.state.run_auth] = lambda: {"sub": "alice"}

    class FakeAgent:
        async def run(self, question):
            return {"status": "limit_reached", "answer": ""}

    with TestClient(app) as client:
        app.state.agent = FakeAgent()
        response = client.post("/research", json={"question": "Research this question"})
        assert response.status_code == 200
        assert app.state.store.get("alice", response.json()["id"]) is not None
        assert client.post("/research", json={"question": "x"}).status_code == 422
