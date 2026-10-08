import json

import httpx
import pytest

from platform_app.agents.research.config import Settings
from platform_app.agents.research.providers import Providers
from platform_app.gateway.llm import LLMError


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai'])
async def test_litellm_tool_roundtrip_preserves_provider_metadata(monkeypatch, provider):
    requests = []
    raw_call = {
        'id': 'call-1', 'type': 'function',
        'function': {'name': 'search_web', 'arguments': '{"query":"oauth pkce"}'},
        'provider_specific_fields': {'thought_signature': 'test-signature'},
    }

    class Response:
        def __init__(self, message, reason):
            self.message, self.reason = message, reason

        def model_dump(self, **kwargs):
            return {'choices': [{'finish_reason': self.reason, 'message': self.message}]}

    async def complete(**kwargs):
        requests.append(kwargs)
        assert kwargs['model'].startswith(provider + '/')
        assert kwargs['api_key'] == 'test-key'
        assert kwargs['num_retries'] == 0
        assert kwargs['timeout'] == 60
        assert '"$ref"' not in json.dumps(kwargs['tools'])
        if len(requests) == 1:
            return Response({'role': 'assistant', 'tool_calls': [raw_call]}, 'tool_calls')
        assert kwargs['messages'][-2]['tool_calls'][0] == raw_call
        assert kwargs['messages'][-1] == {
            'role': 'tool', 'tool_call_id': 'call-1', 'content': '[]'}
        return Response({'role': 'assistant', 'content': 'No evidence found'}, 'stop')

    monkeypatch.setattr('platform_app.gateway.llm.litellm.acompletion', complete)
    settings = Settings('test.auth0.com', 'https://test/api', provider=provider,
                        gemini_key='test-key', anthropic_key='test-key', openai_key='test-key')
    async with httpx.AsyncClient() as client:
        providers = Providers(settings, client)
        messages = [{'role': 'user', 'content': 'Explain PKCE'}]
        blocks = await providers.complete(messages)
        assert blocks[0]['input'] == {'query': 'oauth pkce'}
        messages.extend([
            {'role': 'assistant', 'content': blocks},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': '[]'}]},
        ])
        result = await providers.complete(messages)
        assert result[0]['type'] == 'text'
    assert len(requests) == 2


async def test_litellm_rate_limit_is_sanitized(monkeypatch):
    import litellm

    async def limited(**kwargs):
        raise litellm.RateLimitError('sensitive provider response', 'gemini', 'gemini/test')

    monkeypatch.setattr('platform_app.gateway.llm.litellm.acompletion', limited)
    settings = Settings('test.auth0.com', 'https://test/api', provider='gemini', gemini_key='test-key')
    async with httpx.AsyncClient() as client:
        with pytest.raises(LLMError) as error:
            await Providers(settings, client).complete([{'role': 'user', 'content': 'test'}])
    assert error.value.code == 'upstream_http_429'
    assert 'sensitive' not in str(error.value)
