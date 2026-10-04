"""Code-managed workflows and least-privilege MCP bindings.

Discovery loads schemas only from configured servers. Each agent receives only
its explicitly mapped tool names. Connections are stored per user and server.
The gateway combines tools across servers using server__tool names.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPBinding:
    server: str
    tools: tuple[str, ...]
    required: bool = False


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    description: str
    transport: str = "local"
    endpoint: str = ""
    token_env: str = ""
    connections: tuple[MCPBinding, ...] = ()
    workflow: str = "research"


AGENTS = [
    AgentSpec("research", "Research agent", "Research with sources and verified quotations.",
              connections=(MCPBinding("internal", ("whoami", "list_orders", "get_order")),)),
    # HTTP/A2A agents use their own token_env credential. They never receive
    # user MCP tokens. Add tool bindings only to local workflows that handle them.
    # AgentSpec("remote", "Remote agent", "HTTP workflow", "http",
    #           "http://localhost:8020/research", "REMOTE_AGENT_TOKEN"),
    # AgentSpec("a2a", "A2A agent", "A2A v0.3 workflow", "a2a",
    #           "http://localhost:8030/", "A2A_AGENT_TOKEN"),
]

MCP_SERVERS = {
    "internal": {"name": "Internal tools", "auth": "oauth", "overrides": {}},
    # Register third-party MCP servers once here, then bind them to any agent.
    # "vendor": {"name": "Vendor tools", "auth": "api_key", "token_env": "VENDOR_MCP_KEY",
    #            "overrides": {"mcp_server_url": "https://vendor.example/mcp"}},
}


def validate_registry():
    if len({a.id for a in AGENTS}) != len(AGENTS):
        raise ValueError("Agent IDs must be unique")
    for server in MCP_SERVERS:
        if "__" in server:
            raise ValueError("MCP server IDs cannot contain the tool namespace separator '__'")
    for agent in AGENTS:
        if len({b.server for b in agent.connections}) != len(agent.connections):
            raise ValueError("An agent can bind each MCP server only once")
        if agent.connections and agent.transport != "local":
            raise ValueError("Remote adapters cannot receive platform MCP connections")
        for binding in agent.connections:
            if binding.server not in MCP_SERVERS or not binding.tools:
                raise ValueError("Agent MCP bindings must name a configured server and tool allowlist")
