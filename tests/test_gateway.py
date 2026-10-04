import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from platform_app.gateway.connections import MCPConnectionError
from platform_app.gateway.service import MCPGateway, ConnectionRequired
from platform_app.registry import AgentSpec, MCPBinding
from platform_app.vault import Vault


@pytest.fixture
def gateway(tmp_path):
    agents = [
        AgentSpec('research', 'Research', '', connections=(
            MCPBinding('internal', ('whoami',)), MCPBinding('vendor', ('whoami',)))),
        AgentSpec('orders', 'Orders', '', connections=(MCPBinding('internal', ('get_order',)),)),
        AgentSpec('required', 'Required', '', connections=(MCPBinding('internal', ('whoami',), True),)),
    ]
    settings = SimpleNamespace(domain='test.auth0.com', mcp_server_url='http://localhost:3000/mcp',
                               mcp_client_id='')
    servers = {'internal': {'name': 'Internal'}, 'vendor': {'name': 'Vendor'}}
    vault = Vault(tmp_path)
    for key in servers:
        vault.put('mcp:' + key, {'tokens': {'alice': {'token': 'private', 'expires': time.time() + 600}}})
    return MCPGateway(settings, None, vault, 'http://localhost:8010', agents, servers)


@pytest.mark.asyncio
async def test_multi_server_discovery_and_call_permissions(gateway):
    called = []

    class Remote:
        resource = 'https://tools.example/mcp'
        def describe(self):
            return [{'name': name, 'description': '', 'input_schema': {}}
                    for name in ('whoami', 'get_order', 'forbidden')]
        async def call(self, name, arguments):
            called.append(name)
            return 'verified output'

    @asynccontextmanager
    async def session(owner):
        assert owner == 'alice'
        yield Remote()

    for connection in gateway.connections.values():
        connection.session = session
    async with gateway.session('research', 'alice') as tools:
        assert {t['name'] for t in tools.describe()} == {'internal__whoami', 'vendor__whoami'}
        assert await tools.call('vendor__whoami', {}) == 'verified output'
        with pytest.raises(MCPConnectionError):
            await tools.call('internal__get_order', {})
        with pytest.raises(MCPConnectionError):
            await tools.call('unknown__whoami', {})
        # A policy change during a run also prevents execution.
        gateway.agents['research'] = AgentSpec('research', 'Research', '')
        with pytest.raises(MCPConnectionError):
            await tools.call('internal__whoami', {})
    assert called == ['whoami']
    async with gateway.session('orders', 'alice') as tools:
        assert [t['name'] for t in tools.describe()] == ['internal__get_order']


@pytest.mark.asyncio
async def test_connections_are_not_shared_between_users(gateway):
    async with gateway.session('research', 'bob') as tools:
        assert tools.describe() == []
        with pytest.raises(MCPConnectionError):
            await tools.call('internal__whoami', {})
    with pytest.raises(ConnectionRequired) as exc:
        async with gateway.session('required', 'bob'):
            pytest.fail('Required missing connection must stop execution')
    assert exc.value.server == 'internal'
    assert not any(s['connected'] for s in gateway.status('bob'))
    assert all(s['connected'] for s in gateway.status('alice'))


@pytest.mark.asyncio
async def test_unknown_agent_and_unmapped_server_are_denied(gateway):
    with pytest.raises(MCPConnectionError):
        async with gateway.session('unknown', 'alice'):
            pytest.fail('Unknown agent must be denied')
    with pytest.raises(MCPConnectionError):
        await gateway.connect('unregistered', 'alice')


def test_service_credentials_registered_once_in_gateway(tmp_path, monkeypatch):
    settings = SimpleNamespace(domain='test.auth0.com', mcp_server_url='http://localhost:3000/mcp',
                               mcp_client_id='')
    agent = AgentSpec('a', 'A', '', connections=(MCPBinding('vendor', ('lookup',)),))
    servers = {'vendor': {'name': 'Vendor', 'auth': 'api_key', 'token_env': 'VENDOR_MCP_KEY'}}
    gateway = MCPGateway(settings, None, Vault(tmp_path), 'http://localhost:8010', [agent], servers)
    assert gateway.status('alice')[0]['connected'] is False
    monkeypatch.setenv('VENDOR_MCP_KEY', 'service-secret')
    assert gateway.status('alice')[0]['connected'] is True
    assert gateway.status('alice')[0]['managed'] is True
    assert 'service-secret' not in str(gateway.status('alice'))


@pytest.mark.asyncio
async def test_revoked_oauth_token_prompts_reconnect(gateway, monkeypatch):
    from platform_app.gateway.connections import MCPBridge
    @asynccontextmanager
    async def rejected(self, owner):
        response = httpx.Response(401, request=httpx.Request('POST', 'https://tools.example/mcp'))
        response.raise_for_status()
        yield
    monkeypatch.setattr(MCPBridge, 'session', rejected)
    with pytest.raises(ConnectionRequired) as exc:
        async with gateway.session('research', 'alice'):
            pytest.fail('Revoked token must stop the run')
    assert exc.value.server == 'internal'
    status = next(s for s in gateway.status('alice') if s['id'] == 'internal')
    assert status['connected'] is False and status['expired'] is True
