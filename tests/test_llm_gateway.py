from types import SimpleNamespace

import pytest

from platform_app.gateway.llm import LLMError, LLMGateway, ModelRoute


@pytest.mark.asyncio
async def test_text_only_calls_use_registered_model_and_credentials(monkeypatch):
    requests = []
    async def complete(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_: {
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Hello'}}]})
    monkeypatch.setattr('platform_app.gateway.llm.litellm.acompletion', complete)
    gateway = LLMGateway({'primary': ModelRoute('gemini/test-model', 'backend-secret')}, 'primary')
    result = await gateway.complete([{'role': 'user', 'content': 'Hello'}])
    assert result['content'] == 'Hello'
    assert requests[0]['model'] == 'gemini/test-model'
    assert requests[0]['api_key'] == 'backend-secret'
    assert 'tools' not in requests[0]
    assert requests[0]['num_retries'] == 0
    assert 'backend-secret' not in repr(gateway.routes)


@pytest.mark.asyncio
async def test_unknown_route_and_missing_key_do_not_call_provider(monkeypatch):
    async def unexpected(**kwargs):
        pytest.fail('Unconfigured routes must not call the SDK')
    monkeypatch.setattr('platform_app.gateway.llm.litellm.acompletion', unexpected)
    gateway = LLMGateway({'primary': ModelRoute('gemini/test', '')}, 'primary')
    for provider, code in [('unknown', 'unknown_model_provider'), ('primary', 'missing_provider_key')]:
        with pytest.raises(LLMError) as exc:
            await gateway.complete([], provider=provider)
        assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('timeout', 'upstream_timeout'), ('auth', 'upstream_http_401')])
async def test_provider_errors_are_sanitized(monkeypatch, kind, expected):
    import litellm
    async def fail(**kwargs):
        cls = litellm.Timeout if kind == 'timeout' else litellm.AuthenticationError
        raise cls('private upstream details', 'gemini', 'gemini/test')
    monkeypatch.setattr('platform_app.gateway.llm.litellm.acompletion', fail)
    gateway = LLMGateway({'primary': ModelRoute('gemini/test', 'secret')}, 'primary')
    with pytest.raises(LLMError) as exc:
        await gateway.complete([{'role': 'user', 'content': 'Hello'}])
    assert exc.value.code == expected
    assert 'private' not in str(exc.value)
