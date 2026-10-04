"""Single platform entry point for model calls, credentials and SDK policy."""
import os
from dataclasses import dataclass, field

from openai import APIError

# SDK startup uses its bundled model metadata without a network pricing fetch.
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
import litellm

litellm.telemetry = False
litellm.suppress_debug_info = True


class LLMError(Exception):
    """Only safe error codes cross the gateway boundary."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ModelRoute:
    model: str
    api_key: str = field(repr=False)


class LLMGateway:
    def __init__(self, routes, default_provider):
        if default_provider not in routes:
            raise ValueError('Default model provider must be registered')
        self.routes = dict(routes)
        self.default_provider = default_provider

    @classmethod
    def from_settings(cls, settings):
        routes = {}
        for provider in ('gemini', 'anthropic', 'openai'):
            selected = settings.for_provider(provider)
            model = selected.model
            if not model.startswith(provider + '/'):
                model = provider + '/' + model
            routes[provider] = ModelRoute(model, selected.llm_key)
        return cls(routes, settings.provider)

    def configured(self, provider=None):
        route = self.routes.get(provider or self.default_provider)
        return bool(route and route.api_key)

    async def complete(self, messages, *, tools=None, provider=None):
        """OpenAI-format input/output; agent prompts and loop state stay in agents.

        Models and credentials come exclusively from registered platform routes.
        Retries are disabled so each call counts once against a workflow budget.
        """
        route = self.routes.get(provider or self.default_provider)
        if route is None:
            raise LLMError('unknown_model_provider')
        if not route.api_key:
            raise LLMError('missing_provider_key')
        try:
            response = await litellm.acompletion(
                model=route.model,
                api_key=route.api_key,
                messages=messages,
                **({'tools': tools} if tools else {}),
                max_tokens=2500,
                timeout=60,
                num_retries=0,
            )
        except litellm.Timeout:
            raise LLMError('upstream_timeout') from None
        except APIError as exc:
            status = getattr(exc, 'status_code', None)
            code = f'upstream_http_{status}' if isinstance(status, int) else 'invalid_or_unavailable_upstream'
            raise LLMError(code) from None
        choice = response.model_dump(exclude_none=True)['choices'][0]
        if choice.get('finish_reason') not in {'stop', 'tool_calls'}:
            raise LLMError('incomplete_model_response')
        return choice['message']
