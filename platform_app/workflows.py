"""Add local workflows here. Every workflow receives the shared gateway tool interface.

Signature: async run(question, owner, settings, client, store, tools, llm) -> result.
MCP connections and model SDK calls belong to their respective shared gateways.
"""
from .agents.research.workflow import run as research

LOCAL_WORKFLOWS = {"research": research}
