"""User-bound OAuth DCR/PKCE plus a read-only allowlist of MCP tools."""

import asyncio
import base64
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

import httpx2
from fastapi_plugin import Auth0FastAPI
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class MCPConnectionError(ValueError):
    pass


def validate_url(url):
    parsed = urlsplit(url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
        )
    ):
        raise MCPConnectionError("MCP URLs require HTTPS or explicit loopback HTTP")
    return parsed


class MCPBridge:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.resource = settings.mcp_server_url
        self.issuer = f"https://{settings.domain}/"
        validate_url(self.resource)
        redirect = validate_url(settings.mcp_redirect_uri)
        if redirect.path != "/mcp/callback":
            raise MCPConnectionError("MCP redirect path must be /mcp/callback")
        self.pending, self.clients, self.tokens = {}, {}, {}
        self.lock = asyncio.Lock()
        self.verifier = Auth0FastAPI(domain=settings.domain, audience=self.resource).api_client

    def trusted(self, endpoint):
        parsed = validate_url(endpoint)
        if parsed.scheme != "https" or parsed.netloc != urlsplit(self.issuer).netloc:
            raise MCPConnectionError("OAuth endpoint is outside the trusted Auth0 tenant")
        return endpoint

    def prune(self):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        self.tokens = {k: v for k, v in self.tokens.items() if v["expires"] > now}

    async def connect(self, owner):
        async with self.lock:
            self.prune()
            if len(self.clients) >= 256 and owner not in self.clients:
                raise MCPConnectionError("MCP connection capacity reached")
            challenge = await self.client.post(self.resource, json={})
            match = re.search(
                r'resource_metadata="([^"]+)"', challenge.headers.get("www-authenticate", "")
            )
            if challenge.status_code != 401 or not match:
                raise MCPConnectionError("MCP server did not advertise OAuth resource metadata")
            metadata_url = match.group(1)
            parsed = validate_url(metadata_url)
            origin = urlsplit(self.resource)
            if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                raise MCPConnectionError("Untrusted MCP resource metadata origin")
            response = await self.client.get(metadata_url)
            response.raise_for_status()
            protected = response.json()
            if protected.get("resource") != self.resource or self.issuer not in protected.get(
                "authorization_servers", []
            ):
                raise MCPConnectionError("MCP resource or issuer mismatch")
            response = await self.client.get(self.issuer + ".well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("issuer") != self.issuer or "S256" not in metadata.get(
                "code_challenge_methods_supported", []
            ):
                raise MCPConnectionError("Issuer mismatch or PKCE S256 unsupported")
            for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
                self.trusted(metadata[key])
            client_id = self.clients.get(owner) or self.settings.mcp_client_id
            if not client_id:
                response = await self.client.post(
                    metadata["registration_endpoint"],
                    json={
                        "client_name": "Research Agent MCP",
                        "redirect_uris": [self.settings.mcp_redirect_uri],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code"],
                        "response_types": ["code"],
                    },
                )
                response.raise_for_status()
                registration = response.json()
                if registration.get("token_endpoint_auth_method") != "none":
                    raise MCPConnectionError("Expected a public DCR client")
                client_id = registration["client_id"]
                self.clients[owner] = client_id
            self.pending = {k: v for k, v in self.pending.items() if v["owner"] != owner}
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            self.pending[state] = {
                "owner": owner,
                "verifier": verifier,
                "client_id": client_id,
                "token_endpoint": metadata["token_endpoint"],
                "expires": time.time() + 600,
            }
            return {
                "authorization_url": metadata["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "response_type": "code",
                        "redirect_uri": self.settings.mcp_redirect_uri,
                        "scope": "openid mcp:access",
                        "resource": self.resource,
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                ),
                "expires_in": 600,
            }

    async def callback(self, params):
        self.prune()
        state = params.get("state", "")
        flow = self.pending.pop(state, None)  # Single-use, including rejected callbacks.
        if not flow or params.get("error") or not params.get("code"):
            raise MCPConnectionError("Invalid, expired or denied OAuth callback")
        if params.get("iss", self.issuer) != self.issuer:
            raise MCPConnectionError("Authorization response issuer mismatch")
        response = await self.client.post(
            flow["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": flow["client_id"],
                "code": params["code"],
                "redirect_uri": self.settings.mcp_redirect_uri,
                "code_verifier": flow["verifier"],
                "resource": self.resource,
            },
        )
        response.raise_for_status()
        issued = response.json()
        if issued.get("token_type", "").lower() != "bearer":
            raise MCPConnectionError("Expected a bearer access token")
        token = issued["access_token"]
        claims = await self.verifier.verify_access_token(
            token, required_claims=["sub", "client_id"]
        )
        if (
            claims["sub"] != flow["owner"]
            or claims["client_id"] != flow["client_id"]
            or "mcp:access" not in claims.get("scope", "").split()
            or "cnf" in claims
        ):
            raise MCPConnectionError("MCP token must belong to the same user and registered client")
        self.tokens[flow["owner"]] = {"token": token, "expires": claims["exp"]}

    def token_for(self, owner):
        self.prune()
        if owner not in self.tokens:
            raise MCPConnectionError("Connect MCP first; the connection is missing or expired")
        return self.tokens[owner]["token"]

    def disconnect(self, owner):
        self.tokens.pop(owner, None)
        self.pending = {k: v for k, v in self.pending.items() if v["owner"] != owner}

    @asynccontextmanager
    async def session(self, owner):
        token = self.token_for(owner)
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + token} if token else {},
                timeout=25,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            streamable_http_client(self.resource, http_client=client) as streams,
            ClientSession(streams[0], streams[1], read_timeout_seconds=25) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            allowed = {t.name: t for t in tools if t.name in self.settings.mcp_allowed_tools}
            yield MCPTools(session, allowed, self.resource)


class MCPTools:
    def __init__(self, session, tools, resource):
        self.session, self.tools, self.resource = session, tools, resource

    def describe(self):
        return [
            {
                "name": t.name,
                "description": (t.description or "")[:500],
                "input_schema": t.input_schema,
            }
            for t in self.tools.values()
        ]

    async def call(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            raise MCPConnectionError("MCP tool is not allowlisted")

        def check_refs(node):
            if isinstance(node, dict):
                if "$ref" in node and not node["$ref"].startswith("#"):
                    raise MCPConnectionError("External schema references are unsupported")
                for value in node.values():
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)

        check_refs(tool.input_schema)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except SchemaError:
            raise MCPConnectionError("Invalid MCP tool arguments") from None
        result = await self.session.call_tool(name, arguments)
        if result.is_error:
            raise MCPConnectionError("MCP tool failed")
        # Do not pass images, embedded resources, or server instructions to the model.
        texts = [block.text for block in result.content if block.type == "text"]
        if not texts:
            raise MCPConnectionError("MCP tool returned no text evidence")
        return "\n".join(texts)[:8000]
