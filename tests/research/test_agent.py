import asyncio

import pytest

from platform_app.agents.research.agent import Agent
from platform_app.agents.research.config import Settings

QUOTE = "The source contains a verified research observation."


def call(name, args):
    return [{"type": "tool_use", "id": "test", "name": name, "input": args}]


class FakeProviders:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.searches = 0

    async def complete(self, messages):
        return next(self.responses)

    async def search(self, query):
        self.searches += 1
        return [{"url": "https://example.com/source", "title": "Source", "content": QUOTE}]


def settings(**kwargs):
    return Settings("test.auth0.com", "https://test/api", **kwargs)


def finish(quote=QUOTE, source="S1"):
    return call(
        "finish",
        {
            "claims": [
                {"text": "An observation.", "evidence": [{"source_id": source, "quote": quote}]}
            ]
        },
    )


async def test_research_can_finish_with_verified_sources():
    providers = FakeProviders(
        [
            call("search_web", {"query": "research observation"}),
            call("inspect_source", {"source_id": "S1"}),
            finish(),
        ]
    )
    result = await Agent(settings(), providers).run("What was observed?")
    assert result["status"] == "completed"
    assert "[S1]" in result["answer"]
    assert result["tool_calls"] == 3


@pytest.mark.parametrize(
    "bad", [finish("A fabricated quote absent from the source."), finish(source="S99")]
)
async def test_fabricated_evidence_is_rejected_and_can_be_repaired(bad):
    providers = FakeProviders(
        [call("search_web", {"query": "research observation"}), bad, finish()]
    )
    result = await Agent(settings(), providers).run("What was observed?")
    assert result["status"] == "completed"
    assert result["trace"][1]["ok"] is False


async def test_tool_limit_stops_even_multiple_calls_in_one_turn():
    providers = FakeProviders([call("search_web", {"query": "test query"}) * 4])
    result = await Agent(settings(max_tool_calls=2), providers).run("Research this")
    assert result["status"] == "limit_reached"
    assert providers.searches == 2
    assert result["answer"] == ""


async def test_step_limit_and_unknown_tool():
    providers = FakeProviders([call("execute_shell", {"command": "anything"})])
    result = await Agent(settings(max_steps=1), providers).run("Research this")
    assert result["status"] == "limit_reached"
    assert not result["trace"][0]["ok"]


async def test_timeout():
    class Slow:
        async def complete(self, messages):
            await asyncio.sleep(10)

    result = await Agent(settings(timeout_seconds=1), Slow()).run("Research this")
    assert result["status"] == "timeout"


async def test_plain_text_is_not_treated_as_verified_answer():
    providers = FakeProviders([[{"type": "text", "text": "Unsupported answer"}]])
    result = await Agent(settings(), providers).run("Research this")
    assert result["status"] == "no_final_answer"
    assert not result["answer"]


async def test_upstream_failure_code_is_safe_and_actionable():
    import httpx

    class Unavailable:
        async def complete(self, messages):
            request = httpx.Request("POST", "https://example.com")
            response = httpx.Response(503, request=request, text="sensitive upstream details")
            response.raise_for_status()

    result = await Agent(settings(), Unavailable()).run("Research this")
    assert result["status"] == "provider_error"
    assert result["error_code"] == "upstream_http_503"
    assert "sensitive" not in str(result)


async def test_short_exact_quotes_support_small_structured_results():
    providers = FakeProviders(
        [call("search_web", {"query": "research observation"}), finish("verified")]
    )
    result = await Agent(settings(), providers).run("What was observed?")
    assert result["status"] == "completed"
