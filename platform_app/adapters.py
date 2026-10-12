import os
import uuid

import httpx
from fastapi import HTTPException

from platform_app.gateway.connections import validate_url


async def invoke(spec, question, use_mcp, local, token):
    if spec.transport == "local":
        if callable(local):
            return await local(spec, question, use_mcp)
        response = await local.post("/research", json={"question": question, "use_mcp": use_mcp},
                                    headers={"Authorization": "Bearer " + token})
    else:
        validate_url(spec.endpoint)
        headers = {}
        if spec.token_env:
            credential = os.getenv(spec.token_env)
            if not credential:
                raise HTTPException(503, "Configure this agent's backend credential")
            headers["Authorization"] = "Bearer " + credential
        payload = {"question": question, "use_mcp": use_mcp}
        request_id = uuid.uuid4().hex
        if spec.transport == "a2a":
            payload = {"jsonrpc": "2.0", "id": request_id, "method": "message/send",
                       "params": {"message": {"kind": "message", "role": "user",
                           "messageId": uuid.uuid4().hex,
                           "parts": [{"kind": "text", "text": question}]},
                           "configuration": {"blocking": True}}}
        elif spec.transport != "http":
            raise HTTPException(500, "Unknown agent transport")
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as remote:
            response = await remote.post(spec.endpoint, json=payload, headers=headers)
    if response.status_code >= 400:
        if spec.transport == "local" and response.status_code in {401, 409}:
            raise HTTPException(response.status_code, "Connection expired. Please reconnect.")
        raise HTTPException(502, "Agent request failed. Check the service configuration.")
    result = response.json()
    if spec.transport != "a2a":
        return result
    if result.get("id") != request_id or result.get("error") or "result" not in result:
        raise HTTPException(502, "A2A request failed")
    result = result["result"]
    if result.get("kind") == "task":
        if result.get("status", {}).get("state") != "completed":
            raise HTTPException(502, "A2A agent did not return a completed task; polling is not supported yet")
        parts = [p for a in result.get("artifacts", []) for p in a.get("parts", [])]
    else:
        parts = result.get("parts", [])
    return {"status": "completed", "answer": "\n".join(p["text"] for p in parts if p.get("kind") == "text")}
