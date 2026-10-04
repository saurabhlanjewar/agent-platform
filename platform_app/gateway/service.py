"""One platform gateway owns credentials, discovery, sessions, and agent policy."""
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace

from .connections import MCPBridge, MCPConnectionError


class ConnectionRequired(Exception):
    def __init__(self, server):
        self.server = server
        super().__init__('Reconnect the configured tool service')


@dataclass(frozen=True)
class ServerSettings:
    domain: str
    mcp_server_url: str
    mcp_redirect_uri: str
    mcp_client_id: str = ''
    mcp_allowed_tools: tuple[str, ...] = ()


class PersistentConnection(MCPBridge):
    def __init__(self, settings, client, vault, key):
        super().__init__(settings, client)
        self.vault, self.key = vault, key
        saved = vault.get(key, {})
        self.tokens, self.clients = saved.get('tokens', {}), saved.get('clients', {})
        self.connected_owners = set(saved.get('owners', self.tokens))
        self.prune()

    def save(self):
        self.prune()
        self.connected_owners.update(self.tokens)
        self.vault.put(self.key, {'tokens': self.tokens, 'clients': self.clients,
                                  'owners': sorted(self.connected_owners)})

    async def connect(self, owner):
        result = await super().connect(owner)
        self.save()
        return result

    async def callback(self, params):
        await super().callback(params)
        self.save()

    @asynccontextmanager
    async def session(self, owner):
        try:
            async with super().session(owner) as remote:
                yield remote
        except Exception as exc:
            def unauthorized(error):
                nested = getattr(error, 'exceptions', ())
                response = getattr(error, 'response', None)
                return (getattr(response, 'status_code', None) == 401 or
                        getattr(error, 'status_code', None) == 401 or
                        any(unauthorized(item) for item in nested))
            if unauthorized(exc):
                self.tokens.pop(owner, None)
                self.connected_owners.add(owner)
                self.save()
                raise ConnectionRequired(self.key.removeprefix('mcp:')) from None
            raise


class ServiceConnection(MCPBridge):
    """Explicit code-configured public or backend API-key MCP server.

    Uses the same SDK transport/schema validation as OAuth connections. This is
    a service credential, not a per-user authorization grant.
    """
    def __init__(self, settings, client, auth, token_env):
        super().__init__(settings, client)
        self.auth, self.token_env = auth, token_env
        self.connected_owners = set()

    def token_for(self, owner):
        if self.auth == 'none':
            return ''
        value = os.getenv(self.token_env, '')
        if not value:
            raise MCPConnectionError('Configure the MCP service API key on the backend')
        return value


class MCPGateway:
    def __init__(self, settings, client, vault, origin, agents, servers):
        self.agents = {agent.id: agent for agent in agents}
        self.servers, self.connections = servers, {}
        base = ServerSettings(settings.domain, os.getenv("MCP_SERVER_URL", "http://localhost:3000/mcp"),
                              origin + "/mcp/callback", os.getenv("MCP_CLIENT_ID", ""))
        for key, entry in servers.items():
            # The transport discovers tools; the gateway applies agent policy.
            allowed = tuple(sorted({tool for agent in agents for binding in agent.connections
                                    if binding.server == key for tool in binding.tools}))
            config = replace(base, **(entry.get('overrides', {}) | {'mcp_allowed_tools': allowed}))
            auth = entry.get('auth', 'oauth')
            if auth == 'oauth':
                connection = PersistentConnection(config, client, vault, 'mcp:' + key)
            elif auth in {'api_key', 'none'}:
                connection = ServiceConnection(config, client, auth, entry.get('token_env', ''))
            else:
                raise ValueError('Unknown MCP server authentication mode')
            self.connections[key] = connection

    def policy(self, agent_id):
        if agent_id not in self.agents:
            raise MCPConnectionError('Unknown agent')
        return self.agents[agent_id].connections

    def status(self, owner):
        mapped = {binding.server for agent in self.agents.values() for binding in agent.connections}
        result = []
        for key in sorted(mapped):
            connection = self.connections[key]
            try:
                connection.token_for(owner)
                connected = True
            except MCPConnectionError:
                connected = False
            managed = self.servers[key].get('auth', 'oauth') != 'oauth'
            result.append({'id': key, 'name': self.servers[key]['name'], 'connected': connected,
                           'managed': managed,
                           'expired': not connected and owner in connection.connected_owners})
        return result

    async def connect(self, server, owner):
        if server not in {binding.server for a in self.agents.values() for binding in a.connections}:
            raise MCPConnectionError('Unknown or unmapped server')
        if self.servers[server].get('auth', 'oauth') != 'oauth':
            raise MCPConnectionError('This connection is managed by the platform configuration')
        return await self.connections[server].connect(owner)

    async def callback(self, params, owner):
        for connection in self.connections.values():
            flow = connection.pending.get(params.get('state', ''))
            if flow and flow['owner'] == owner:
                await connection.callback(params)
                return
        raise MCPConnectionError('Invalid OAuth callback')

    @asynccontextmanager
    async def session(self, agent_id, owner):
        """Credentials and raw transport sessions never enter an agent workflow."""
        routes = {}
        async with AsyncExitStack() as stack:
            for binding in self.policy(agent_id):
                connection = self.connections[binding.server]
                try:
                    connection.token_for(owner)
                except MCPConnectionError:
                    if binding.required or owner in connection.connected_owners:
                        raise ConnectionRequired(binding.server) from None
                    continue
                remote = await stack.enter_async_context(connection.session(owner))
                for tool in remote.describe():
                    if tool['name'] in binding.tools:
                        name = binding.server + '__' + tool['name']
                        routes[name] = (binding.server, tool['name'], remote, tool)
            yield AgentTools(self, agent_id, routes)


class AgentTools:
    """Small credential-free tool interface injected into any local workflow."""
    def __init__(self, gateway, agent_id, routes):
        self.gateway, self.agent_id, self.routes = gateway, agent_id, routes

    def describe(self):
        return [dict(tool, name=name) for name, (_, _, _, tool) in self.routes.items()]

    def resource_for(self, name):
        if name not in self.routes:
            raise MCPConnectionError('Tool is not allowed for this agent')
        return self.routes[name][2].resource

    async def call(self, name, arguments):
        route = self.routes.get(name)
        if route is None:
            raise MCPConnectionError('Tool is not allowed for this agent')
        server, tool, remote, _ = route
        # Enforce again at execution time; schema validation remains in the connector.
        if not any(b.server == server and tool in b.tools for b in self.gateway.policy(self.agent_id)):
            raise MCPConnectionError('Tool access was removed from this agent')
        return await remote.call(tool, arguments)
