import asyncio
import base64
import hashlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .workflows import LOCAL_WORKFLOWS
from .gateway.llm import LLMGateway
from platform_app.agents.research.store import Store
from platform_app.agents.research.config import Settings
from platform_app.gateway.connections import MCPConnectionError
from platform_app.gateway.service import MCPGateway, ConnectionRequired
from .adapters import invoke
from .registry import AGENTS, MCP_SERVERS, validate_registry
from .vault import Vault

ROOT = Path(__file__).resolve().parents[1]


class Chat(BaseModel):
    agent: str = "research"
    message: str = Field(min_length=5, max_length=2000)
    model_config = {"extra": "forbid"}


def create_app():
    load_dotenv(ROOT / ".env", override=True)
    validate_registry()
    if any(a.transport == "local" and a.workflow not in LOCAL_WORKFLOWS for a in AGENTS):
        raise ValueError("Register the local workflow before binding an agent to it")
    settings = replace(Settings.load(), database=str(ROOT / "data/research.sqlite3"))
    origin = os.getenv("PLATFORM_ORIGIN", "http://localhost:8010").rstrip("/")
    client_id = os.getenv("PLATFORM_CLIENT_ID", "")
    vault = Vault(ROOT / "data")
    flows = {}

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            app.state.client = client
            app.state.llm = LLMGateway.from_settings(settings)
            app.state.gateway = MCPGateway(settings, client, vault, origin, AGENTS, MCP_SERVERS)
            app.state.store = Store(settings.database)
            app.state.slots = asyncio.Semaphore(2)
            yield

    app = FastAPI(title="Agent Lab", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def browser_guard(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied_origin = request.headers.get("origin")
            valid = supplied_origin == origin
            # Some browsers omit/null the Origin on native form submissions
            # under our no-referrer policy. Logout also has a session-bound CSRF token.
            if not valid and supplied_origin in {None, "null"} and request.url.path == "/auth/logout":
                try:
                    user = session(request)
                    form = await request.form()
                    expected = user.get("logout_csrf", "")
                    submitted = form.get("csrf", "")
                    valid = bool(expected) and isinstance(submitted, str) and secrets.compare_digest(expected, submitted)
                except (HTTPException, ValueError):
                    pass
            if not valid:
                return PlainTextResponse("Untrusted origin", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def session(request):
        sid = request.cookies.get("agent_session", "")
        value = vault.get("session:" + sid)
        if not value or value["expires"] <= time.time():
            if value:
                vault.delete("session:" + sid)
            raise HTTPException(401, "Please reconnect your account.")
        return value

    @app.get("/")
    async def index(request: Request):
        try:
            user = session(request)
        except HTTPException:
            return RedirectResponse("/login")
        if "logout_csrf" not in user:
            user["logout_csrf"] = secrets.token_urlsafe(32)
            vault.put("session:" + request.cookies["agent_session"], user)
        markup = (ROOT / "platform_app/static/index.html").read_text()
        return HTMLResponse(markup.replace("__LOGOUT_CSRF__", user["logout_csrf"]))

    @app.get("/login")
    async def login_page(request: Request):
        try:
            session(request)
        except HTTPException:
            return FileResponse(ROOT / "platform_app/static/login.html")
        return RedirectResponse("/")

    @app.post("/auth/logout")
    async def logout(request: Request):
        sid = request.cookies.get("agent_session", "")
        vault.delete("session:" + sid)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("agent_session")
        return response

    @app.get("/api/state")
    async def state(request: Request):
        user = session(request)
        connections = app.state.gateway.status(user["sub"])
        return {"authenticated": True,
                "agents": [{"id": a.id, "name": a.name, "description": a.description,
                            "connections": [{"id": b.server, "required": b.required,
                                             "tools": list(b.tools)} for b in a.connections]}
                           for a in AGENTS],
                "connections": connections,
                "model": settings.for_provider().model, "provider": settings.provider}

    @app.get("/auth/connect")
    async def login():
        if not client_id:
            raise HTTPException(503, "Set PLATFORM_CLIENT_ID in agent-platform/.env")
        now = time.time()
        for key in list(flows):
            if flows[key]["expires"] < now:
                flows.pop(key)
        if len(flows) >= 100:
            raise HTTPException(429, "Too many pending logins")
        state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
        flows[state] = {"verifier": verifier, "browser": browser, "expires": now + 600}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        url = f"https://{settings.domain}/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": origin + "/auth/callback",
            "audience": settings.audience, "scope": "openid research:run research:read",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        response = RedirectResponse(url)
        response.set_cookie("login_binding", browser, httponly=True, samesite="lax", max_age=600,
                            secure=origin.startswith("https:"))
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        flow = flows.pop(request.query_params.get("state", ""), None)
        if (not flow or flow["expires"] < time.time() or
                not secrets.compare_digest(flow["browser"], request.cookies.get("login_binding", ""))):
            raise HTTPException(400, "Login expired. Please reconnect.")
        try:
            response = await app.state.client.post(f"https://{settings.domain}/oauth/token", data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": request.query_params["code"], "code_verifier": flow["verifier"],
                "redirect_uri": origin + "/auth/callback"})
            response.raise_for_status()
            issued = response.json()
            if issued.get("token_type", "").lower() != "bearer":
                raise ValueError("Expected bearer token")
            from fastapi_plugin import Auth0FastAPI
            claims = await Auth0FastAPI(domain=settings.domain, audience=settings.audience).api_client.verify_access_token(
                issued["access_token"], required_claims=["sub", "exp"])
            if "research:run" not in claims.get("scope", "").split():
                raise ValueError("Missing research permission")
        except Exception:
            return PlainTextResponse("Login failed. Check client grants and reconnect.", status_code=400)
        sid = secrets.token_urlsafe(32)
        vault.put("session:" + sid, {"token": issued["access_token"], "sub": claims["sub"], "expires": claims["exp"]})
        result = RedirectResponse("/")
        result.delete_cookie("login_binding")
        result.set_cookie("agent_session", sid, httponly=True, samesite="lax",
                          secure=origin.startswith("https:"), max_age=max(1, int(claims["exp"] - time.time())))
        return result

    @app.post("/api/connections/{key}/connect")
    async def connect(key: str, request: Request):
        user = session(request)
        try:
            return await app.state.gateway.connect(key, user["sub"])
        except Exception:
            raise HTTPException(502, "Connection setup failed. Check MCP service and OAuth grants.") from None

    @app.get("/mcp/callback")
    async def mcp_callback(request: Request):
        user = session(request)
        try:
            await app.state.gateway.callback(dict(request.query_params), user["sub"])
            return RedirectResponse("/")
        except Exception:
            return PlainTextResponse("Connection failed. Reconnect with the same account.", status_code=400)

    @app.post("/api/chat")
    async def chat(body: Chat, request: Request):
        user = session(request)
        spec = next((a for a in AGENTS if a.id == body.agent), None)
        if not spec:
            raise HTTPException(404, "Unknown agent")
        async def local_workflow(agent, question, use_mcp):
            async with asyncio.timeout(settings.timeout_seconds):
                async with app.state.gateway.session(agent.id, user["sub"]) as tools:
                    return await LOCAL_WORKFLOWS[agent.workflow](question, user["sub"], settings, app.state.client,
                                              app.state.store, tools, app.state.llm)

        try:
            await asyncio.wait_for(app.state.slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(429, "Agent capacity reached. Please retry later.") from None
        try:
            return await invoke(spec, body.message, False, local_workflow, user["token"])
        except ConnectionRequired as exc:
            raise HTTPException(409, {"message": "Connect this agent's tools to continue.",
                                      "connection": exc.server}) from None
        except TimeoutError:
            raise HTTPException(504, "Agent workflow timed out.") from None
        except (httpx.HTTPError, ExceptionGroup, MCPConnectionError):
            raise HTTPException(502, "Tool gateway or agent service unavailable. Check its connection.") from None
        finally:
            app.state.slots.release()

    @app.get("/static/{filename}")
    async def asset(filename: str):
        if filename not in {"style.css", "app.js"}:
            raise HTTPException(404, "Not found")
        return FileResponse(ROOT / "platform_app/static" / filename)
    return app
