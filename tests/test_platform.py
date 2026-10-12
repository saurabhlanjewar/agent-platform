import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_app.vault import Vault
from platform_app.adapters import invoke
from platform_app.registry import AgentSpec


def test_encrypted_persistence(tmp_path):
    vault = Vault(tmp_path)
    vault.put('alice', {'token': 'private-token'})
    assert b'private-token' not in vault.path.read_bytes()
    assert Vault(tmp_path).get('alice')['token'] == 'private-token'
    assert vault.get('bob') is None
    vault.delete('alice')
    assert vault.get('alice') is None


def test_local_adapter_and_expired_connection():
    async def run():
        def respond(request):
            assert request.headers['authorization'] == 'Bearer user-token'
            assert json.loads(request.content)['use_mcp']
            return httpx.Response(409)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://local') as client:
            with pytest.raises(HTTPException) as error:
                await invoke(AgentSpec('r', 'Research', ''), 'question', True, client, 'user-token')
            assert error.value.status_code == 409
    asyncio.run(run())


def test_a2a_completed_task_and_no_user_token_forwarding(monkeypatch):
    from platform_app import adapters
    client_type = httpx.AsyncClient

    def respond(request):
        assert 'authorization' not in request.headers
        body = json.loads(request.content)
        assert body['method'] == 'message/send'
        assert body['params']['message']['parts'][0]['text'] == 'test question'
        return httpx.Response(200, json={
            'jsonrpc': '2.0', 'id': body['id'], 'result': {
                'kind': 'task', 'status': {'state': 'completed'},
                'artifacts': [{'parts': [{'kind': 'text', 'text': 'Agent answer'}]}]}})

    monkeypatch.setattr(adapters.httpx, 'AsyncClient',
                        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(invoke(AgentSpec('a', 'A2A', '', 'a2a', 'http://localhost:8030/'),
                               'test question', False, None, 'private-user-token'))
    assert result['answer'] == 'Agent answer'


def test_app_registry_auth_and_origin_guard(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from platform_app.agents.research.config import Settings
    from platform_app import app as platform
    monkeypatch.setattr(platform.Settings, 'load', lambda: Settings(
        domain='example.auth0.com', audience='https://research.example'))
    monkeypatch.setattr(platform, 'Vault', lambda _: Vault(tmp_path))
    with TestClient(platform.create_app()) as client:
        assert client.get('/').url.path == '/login'
        assert 'Your workflow playground.' in client.get('/').text
        assert client.get('/api/state').status_code == 401
        assert client.get('/static/index.html').status_code == 404
        assert client.post('/api/chat', json={'message': 'test question'}).status_code == 403
        assert client.post('/api/chat', json={'message': 'test question'},
                           headers={'Origin': 'http://localhost:8010'}).status_code == 401
        assert client.get('/auth/callback?state=invalid&code=invalid').status_code == 400


def test_platform_env_overrides_inherited_model(monkeypatch, tmp_path):
    from platform_app import app as platform
    from fastapi.testclient import TestClient
    (tmp_path / '.env').write_text("GEMINI_MODEL=gemini-3.1-flash-lite\nLLM_PROVIDER=gemini\n")
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-3.5-flash')
    monkeypatch.setenv('AUTH0_DOMAIN', 'example.auth0.com')
    monkeypatch.setenv('AUTH0_AUDIENCE', 'https://research.example')
    monkeypatch.setattr(platform, 'ROOT', tmp_path)
    monkeypatch.setattr(platform, 'Vault', lambda _: Vault(tmp_path / 'vault'))
    (tmp_path / 'platform_app/static').mkdir(parents=True)
    with TestClient(platform.create_app()) as client:
        vault = Vault(tmp_path / 'vault')
        vault.put('session:test', {'token': 'test', 'sub': 'alice', 'expires': time.time() + 600})
        client.cookies.set('agent_session', 'test')
        assert client.get('/api/state').json()['model'] == 'gemini-3.1-flash-lite'


def test_agent_mapping_and_connection_authorization(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from platform_app import app as platform
    from platform_app.registry import AgentSpec, MCPBinding
    from platform_app.agents.research.config import Settings
    vault = Vault(tmp_path)
    vault.put('session:test', {'token': 'private', 'sub': 'alice', 'expires': time.time() + 600})
    vault.put('mcp:internal', {'tokens': {'alice': {'token': 'mcp-token', 'expires': time.time() + 600}}})
    monkeypatch.setattr(platform, 'Vault', lambda _: vault)
    monkeypatch.setattr(platform.Settings, 'load', lambda: Settings('example.auth0.com', 'https://api.example'))
    monkeypatch.setattr(platform, 'AGENTS', [
        AgentSpec('research', 'Research', '', connections=(MCPBinding('internal', ('whoami',)),)),
        AgentSpec('plain', 'Plain', '')])
    observed = []

    from contextlib import asynccontextmanager
    class Remote:
        resource = 'http://localhost:3000/mcp'
        def describe(self):
            return [{'name': name, 'description': '', 'input_schema': {}}
                    for name in ('whoami', 'get_order')]

    @asynccontextmanager
    async def tool_session(self, owner):
        yield Remote()

    async def fake_run(question, owner, settings, client, store, tools, llm):
        names = [item['name'] for item in tools.describe()]
        observed.append(names)
        return {'answer': 'done'}

    monkeypatch.setattr('platform_app.gateway.service.PersistentConnection.session', tool_session)
    monkeypatch.setitem(platform.LOCAL_WORKFLOWS, 'research', fake_run)
    with TestClient(platform.create_app()) as client:
        client.cookies.set('agent_session', 'test')
        assert client.get('/').url.path == '/'
        assert client.get('/login').url.path == '/'
        state = client.get('/api/state').json()
        assert state['agents'][0]['connections'][0]['tools'] == ['whoami']
        assert state['agents'][1]['connections'] == []
        headers = {'Origin': 'http://localhost:8010'}
        assert client.post('/api/chat', headers=headers,
                           json={'agent': 'research', 'message': 'test question'}).status_code == 200
        assert client.post('/api/chat', headers=headers,
                           json={'agent': 'plain', 'message': 'test question'}).status_code == 200
        assert observed == [['internal__whoami'], []]
        assert client.post('/api/chat', headers=headers, json={
            'agent': 'plain', 'message': 'test question', 'connection': 'internal'}).status_code == 422
        assert client.post('/auth/logout', headers=headers, follow_redirects=False).status_code == 303
        assert client.get('/api/state').status_code == 401


@pytest.mark.parametrize("expired", [False, True])
def test_required_or_expired_connection_prompts_without_running_agent(monkeypatch, tmp_path, expired):
    from fastapi.testclient import TestClient
    from platform_app import app as platform
    from platform_app.registry import AgentSpec, MCPBinding
    from platform_app.agents.research.config import Settings
    vault = Vault(tmp_path)
    vault.put('session:test', {'token': 'private', 'sub': 'alice', 'expires': time.time() + 600})
    if expired:
        vault.put('mcp:internal', {'tokens': {'alice': {'token': 'old', 'expires': time.time() - 5}}})
    monkeypatch.setattr(platform, 'Vault', lambda _: vault)
    monkeypatch.setattr(platform.Settings, 'load', lambda: Settings('example.auth0.com', 'https://api.example'))
    monkeypatch.setattr(platform, 'AGENTS', [
        AgentSpec('research', 'Research', '', connections=(MCPBinding('internal', ('whoami',), required=not expired),))])
    with TestClient(platform.create_app()) as client:
        client.cookies.set('agent_session', 'test')
        response = client.post('/api/chat', headers={'Origin': 'http://localhost:8010'},
                               json={'message': 'test question'})
        assert response.status_code == 409
        assert response.json()['detail']['connection'] == 'internal'


@pytest.mark.parametrize('origin', [None, 'null'])
def test_logout_form_without_browser_origin_requires_csrf(monkeypatch, tmp_path, origin):
    import re
    from fastapi.testclient import TestClient
    from platform_app import app as platform
    from platform_app.agents.research.config import Settings
    vault = Vault(tmp_path)
    vault.put('session:test', {'token': 'private', 'sub': 'alice', 'expires': time.time() + 600})
    monkeypatch.setattr(platform, 'Vault', lambda _: vault)
    monkeypatch.setattr(platform.Settings, 'load', lambda: Settings('example.auth0.com', 'https://api.example'))
    with TestClient(platform.create_app()) as client:
        client.cookies.set('agent_session', 'test')
        html = client.get('/').text
        csrf = re.search('name="csrf" value="([^"]+)"', html).group(1)
        headers = {'Origin': origin} if origin else {}
        assert client.post('/auth/logout', headers=headers, data={'csrf': 'wrong'}).status_code == 403
        assert client.post('/auth/logout', headers={'Origin': 'https://evil.example'},
                           data={'csrf': csrf}).status_code == 403
        response = client.post('/auth/logout', headers=headers, data={'csrf': csrf})
        assert response.url.path == '/login'
        assert vault.get('session:test') is None
        assert client.get('/api/state').status_code == 401
