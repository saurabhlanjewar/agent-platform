import asyncio
import json
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi_plugin import Auth0FastAPI

from platform_app.gateway.llm import LLMGateway

from .agent import Agent
from .config import Settings
from platform_app.gateway.service import ConnectionRequired
from platform_app.gateway.connections import MCPConnectionError
from .models import MCP_TOOL, TOOLS, Question
from .providers import Providers
from .store import Store


def create_app(settings=None, tool_gateway=None):
    settings = settings or Settings.load()
    auth = Auth0FastAPI(domain=settings.domain, audience=settings.audience)
    run_auth = auth.require_auth(scopes="research:run")
    read_auth = auth.require_auth(scopes="research:read")

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            app.state.client = client
            app.state.llm = LLMGateway.from_settings(settings)
            app.state.agent = Agent(settings, Providers(settings, client, llm=app.state.llm))
            app.state.store = Store(settings.database)
            app.state.slots = asyncio.Semaphore(2)
            yield

    app = FastAPI(title="Research Agent", version="0.1.0", lifespan=lifespan)
    # References allow authorization tests without adding any runtime auth bypass.
    app.state.run_auth, app.state.read_auth = run_auth, read_auth

    def subject(claims):
        value = claims.get("sub")
        if not isinstance(value, str) or not value:
            raise HTTPException(401, "Token subject is required")
        return value

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/research")
    async def research(body: Question, claims: Annotated[dict, Depends(run_auth)]):
        owner = subject(claims)
        selected = settings.for_provider(body.provider)
        if not selected.llm_key or (not body.use_mcp and not selected.tavily_key):
            raise HTTPException(
                503, f"Configure the {selected.provider} API key and TAVILY_API_KEY"
            )
        if body.use_mcp:
            try:
                if tool_gateway is None:
                    raise MCPConnectionError("Connect tools through the platform gateway")
            except MCPConnectionError as exc:
                raise HTTPException(409, str(exc)) from None
        try:
            await asyncio.wait_for(app.state.slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(
                429, "Research capacity reached; retry later", headers={"Retry-After": "5"}
            ) from None
        try:
            agent = (
                Agent(selected, Providers(selected, app.state.client, llm=app.state.llm))
                if body.provider
                else app.state.agent
            )
            if body.use_mcp:
                try:
                    async with asyncio.timeout(selected.timeout_seconds):
                        async with tool_gateway.session("research", owner) as remote:
                            mcp_tool = {
                                **MCP_TOOL,
                                "description": MCP_TOOL["description"]
                                + " Available tools: "
                                + json.dumps(remote.describe()),
                            }
                            tools = [
                                t for t in TOOLS if t["name"] != "search_web" or selected.tavily_key
                            ]
                            providers = Providers(
                                selected, app.state.client, tools=tools + [mcp_tool], llm=app.state.llm
                            )
                            result = await Agent(selected, providers, mcp=remote).run(body.question)
                except TimeoutError:
                    raise HTTPException(504, "MCP research timed out") from None
                except (
                    ExceptionGroup,
                    MCPConnectionError,
                    ConnectionRequired,
                    ValueError,
                    KeyError,
                    TypeError,
                ):
                    raise HTTPException(
                        502, "MCP research failed; check connection and server grants"
                    ) from None
            else:
                result = await agent.run(body.question)
            result["provider"] = selected.provider
            result["model"] = selected.model
            return await asyncio.to_thread(app.state.store.save, owner, body.question, result)
        finally:
            app.state.slots.release()

    @app.get("/research")
    async def history(claims: Annotated[dict, Depends(read_auth)]):
        return {"runs": await asyncio.to_thread(app.state.store.list, subject(claims))}

    @app.get("/research/{run_id}")
    async def detail(run_id: str, claims: Annotated[dict, Depends(read_auth)]):
        result = await asyncio.to_thread(app.state.store.get, subject(claims), run_id)
        if result is None:
            raise HTTPException(404, "Research not found")
        return result

    return app
