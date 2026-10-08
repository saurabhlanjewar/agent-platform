import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent import QUOTE, FakeProviders, call, finish
from test_api import make_app

from platform_app.agents.research.agent import Agent
from platform_app.agents.research.config import Settings
from platform_app.gateway.connections import MCPBridge, MCPConnectionError, MCPTools
from platform_app.gateway.service import ServerSettings

ISSUER = "https://test.auth0.com/"
RESOURCE = "http://localhost:3000/mcp"


class OAuthServer:
    def __init__(self):
        self.requests = []
        self.registrations = 0
        self.external_endpoint = False

    def handle(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url == RESOURCE:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="http://localhost:3000/.well-known/oauth-protected-resource"'
                },
            )
        if url.startswith("http://localhost:3000/.well-known/"):
            return httpx.Response(
                200, json={"resource": RESOURCE, "authorization_servers": [ISSUER]}
            )
        if url == ISSUER + ".well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "code_challenge_methods_supported": ["S256"],
                    "authorization_endpoint": ISSUER + "authorize",
                    "token_endpoint": ISSUER + "oauth/token",
                    "registration_endpoint": "https://evil.example/register"
                    if self.external_endpoint
                    else ISSUER + "oidc/register",
                },
            )
        if url == ISSUER + "oidc/register":
            self.registrations += 1
            assert json.loads(request.content)["token_endpoint_auth_method"] == "none"
            return httpx.Response(
                201, json={"client_id": "dynamic-client", "token_endpoint_auth_method": "none"}
            )
        if url == ISSUER + "oauth/token":
            data = parse_qs(request.content.decode())
            assert len(data["code_verifier"][0]) >= 43
            assert data["resource"] == [RESOURCE]
            return httpx.Response(
                200, json={"access_token": "mcp-only-token", "token_type": "Bearer"}
            )
        raise AssertionError("Unexpected endpoint " + url)


async def bridge_fixture(owner="alice", external=False):
    server = OAuthServer()
    server.external_endpoint = external
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    bridge = MCPBridge(ServerSettings("test.auth0.com", RESOURCE, "http://localhost:8002/mcp/callback",
                                         mcp_allowed_tools=("whoami", "list_orders", "get_order")), client)

    async def verify(token, required_claims):
        assert token == "mcp-only-token"
        return {
            "sub": owner,
            "client_id": "dynamic-client",
            "scope": "mcp:access",
            "exp": 9999999999,
        }

    bridge.verifier = SimpleNamespace(verify_access_token=verify)
    return bridge, server, client


async def test_dcr_pkce_callback_and_user_isolation():
    bridge, server, client = await bridge_fixture()
    async with client:
        result = await bridge.connect("alice")
        query = parse_qs(urlsplit(result["authorization_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [RESOURCE]
        assert "code_verifier" not in query
        params = {"state": query["state"][0], "code": "code", "iss": ISSUER}
        await bridge.callback(params)
        assert bridge.token_for("alice") == "mcp-only-token"
        with pytest.raises(MCPConnectionError):
            bridge.token_for("bob")
        with pytest.raises(MCPConnectionError):
            await bridge.callback(params)
        await bridge.connect("alice")
        assert server.registrations == 1
        bridge.disconnect("alice")
        with pytest.raises(MCPConnectionError):
            bridge.token_for("alice")
        assert not bridge.pending


async def test_different_login_identity_is_rejected():
    bridge, _, client = await bridge_fixture(owner="bob")
    async with client:
        url = (await bridge.connect("alice"))["authorization_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        with pytest.raises(MCPConnectionError):
            await bridge.callback({"state": state, "code": "code"})
        assert not bridge.tokens


async def test_untrusted_discovery_endpoint_is_never_called():
    bridge, server, client = await bridge_fixture(external=True)
    async with client:
        with pytest.raises(MCPConnectionError):
            await bridge.connect("alice")
        assert all(request.url.host != "evil.example" for request in server.requests)
        assert server.registrations == 0


async def test_invalid_and_expired_states_do_not_exchange_code():
    bridge, server, client = await bridge_fixture()
    async with client:
        await bridge.connect("alice")
        for flow in bridge.pending.values():
            flow["expires"] = 0
        with pytest.raises(MCPConnectionError):
            await bridge.callback({"state": "unknown", "code": "code"})
        assert not any(str(r.url).endswith("oauth/token") for r in server.requests)


async def test_mcp_tool_allowlist_schema_and_errors():
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                is_error=False, content=[SimpleNamespace(type="text", text=QUOTE)]
            )

    tools = {
        "get_order": SimpleNamespace(
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "integer"}},
                "required": ["order_id"],
                "additionalProperties": False,
            }
        )
    }
    remote = MCPTools(Session(), tools, RESOURCE)
    with pytest.raises(MCPConnectionError):
        await remote.call("delete_order", {})
    with pytest.raises(MCPConnectionError):
        await remote.call("get_order", {"order_id": "invalid"})
    assert calls == []
    assert await remote.call("get_order", {"order_id": 1}) == QUOTE


async def test_mcp_results_can_be_cited_and_consume_budget():
    class Remote:
        resource = RESOURCE

        async def call(self, name, arguments):
            return QUOTE

    providers = FakeProviders([call("call_mcp_tool", {"name": "list_orders"}), finish()])
    settings = Settings("test.auth0.com", "https://test/api")
    result = await Agent(settings, providers, mcp=Remote()).run("Research my orders")
    assert result["status"] == "completed"
    assert result["sources"][0]["kind"] == "mcp"
    assert result["tool_calls"] == 2


def test_research_api_does_not_own_mcp_connections(tmp_path):
    app = make_app(tmp_path)
    app.dependency_overrides[app.state.run_auth] = lambda: {"sub": "alice"}
    with TestClient(app) as client:
        assert client.get("/mcp/status").status_code == 404
        assert client.post("/mcp/connect").status_code == 404
        response = client.post("/research", json={"question": "Research orders", "use_mcp": True})
        assert response.status_code == 409


async def test_official_mcp_transport_initializes_lists_and_calls(monkeypatch):
    import httpx2
    from mcp.server.mcpserver import MCPServer

    from platform_app.gateway import connections as mcp_bridge

    server = MCPServer("Research transport test")

    @server.tool()
    async def list_orders() -> str:
        return QUOTE

    @server.tool()
    async def forbidden_tool() -> str:
        return "Must not be available"

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    original_client = httpx2.AsyncClient
    requests = []

    class Transport(httpx2.ASGITransport):
        async def handle_async_request(self, request):
            requests.append(request)
            assert request.headers["authorization"] == "Bearer mcp-only-token"
            return await super().handle_async_request(request)

    def client_factory(**kwargs):
        return original_client(transport=Transport(app=app), **kwargs)

    bridge, _, discovery_client = await bridge_fixture()
    bridge.tokens["alice"] = {"token": "mcp-only-token", "expires": 9999999999}
    monkeypatch.setattr(mcp_bridge.httpx2, "AsyncClient", client_factory)
    async with (
        discovery_client,
        app.router.lifespan_context(app),
        bridge.session("alice") as remote,
    ):
        assert [t["name"] for t in remote.describe()] == ["list_orders"]
        assert QUOTE in await remote.call("list_orders", {})
    assert len(requests) >= 3
