"""Research workflow: receives a generic tool interface, never MCP credentials."""
import asyncio
import json

from .agent import Agent
from .models import MCP_TOOL, TOOLS
from .providers import Providers


async def run(question, owner, settings, client, store, tools, llm):
    selected = settings.for_provider()
    descriptions = tools.describe()
    if not llm.configured(selected.provider) or (not descriptions and not selected.tavily_key):
        from fastapi import HTTPException
        raise HTTPException(503, 'Configure the model API key and a research tool source')
    definitions = [tool for tool in TOOLS if tool['name'] != 'search_web' or selected.tavily_key]
    if descriptions:
        definitions.append(dict(MCP_TOOL, description=MCP_TOOL['description'] +
                                ' Available tools: ' + json.dumps(descriptions)))
    providers = Providers(selected, client, tools=definitions, llm=llm)
    result = await Agent(selected, providers, mcp=tools if descriptions else None).run(question)
    result.update(provider=selected.provider, model=selected.model)
    return await asyncio.to_thread(store.save, owner, question, result)
