"""Explicit model -> validated tool -> observation loop; no orchestration framework."""

import asyncio
import json
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .models import TOOL_MODELS, MCPArgs
from platform_app.gateway.llm import LLMError


class Agent:
    def __init__(self, settings, providers, mcp=None):
        self.settings, self.providers = settings, providers
        self.mcp = mcp

    async def run(self, question):
        state = {
            "status": "running",
            "answer": "",
            "claims": [],
            "limitations": [],
            "sources": {},
            "trace": [],
            "model_calls": 0,
            "tool_calls": 0,
        }
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                await self._loop(question, state)
        except TimeoutError:
            state["status"] = "timeout"
        except LLMError as exc:
            state["status"] = "provider_error"
            state["error_code"] = exc.code
        except httpx.HTTPStatusError as exc:
            state["status"] = "provider_error"
            state["error_code"] = f"upstream_http_{exc.response.status_code}"
        except httpx.TimeoutException:
            state["status"] = "provider_error"
            state["error_code"] = "upstream_timeout"
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            state["status"] = "provider_error"
            state["error_code"] = "invalid_or_unavailable_upstream"
        if state["status"] == "running":
            state["status"] = "limit_reached"
        if state["status"] != "completed":
            state["limitations"].append("Research ended without a verified final answer.")
        state["sources"] = list(state["sources"].values())
        return state

    async def _loop(self, question, state):
        messages = [
            {
                "role": "user",
                "content": (
                    f"Budget: {self.settings.max_steps} model calls; "
                    f"{self.settings.max_tool_calls} tool calls. Question: {question}"
                ),
            }
        ]
        for _ in range(self.settings.max_steps):
            state["model_calls"] += 1
            blocks = await self.providers.complete(messages)
            messages.append({"role": "assistant", "content": blocks})
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            if not calls:
                state["status"] = "no_final_answer"
                return
            observations = []
            for call in calls:
                if state["tool_calls"] >= self.settings.max_tool_calls:
                    return
                state["tool_calls"] += 1
                name = call.get("name", "")
                try:
                    model = (
                        MCPArgs if name == "call_mcp_tool" and self.mcp else TOOL_MODELS.get(name)
                    )
                    if model is None:
                        raise ValueError("Unknown tool")
                    args = model.model_validate(call.get("input"))
                    result = await self._execute(name, args, state)
                    failed = False
                except ValidationError as exc:
                    result, failed = (
                        {
                            "error": "Invalid tool arguments",
                            "fields": [
                                {"field": ".".join(map(str, e["loc"])), "type": e["type"]}
                                for e in exc.errors()
                            ],
                        },
                        True,
                    )
                except (ValueError, KeyError):
                    result, failed = (
                        {
                            "error": "Unknown tool/source or unsupported evidence. "
                            "Quotes must be exact nonempty substrings of stored source text; "
                            "use inspect_source to copy them without paraphrasing."
                        },
                        True,
                    )
                state["trace"].append({"tool": name, "ok": not failed})
                if state["status"] == "completed":
                    return
                observations.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": json.dumps(result),
                        "is_error": failed,
                    }
                )
            messages.append({"role": "user", "content": observations})

    async def _execute(self, name, args, state):
        sources = state["sources"]
        if name == "call_mcp_tool" and self.mcp:
            if len(sources) >= 24:
                raise ValueError("Source limit reached")
            text = await self.mcp.call(args.name, args.arguments)
            sid = f"S{len(sources) + 1}"
            source = {
                "id": sid,
                "url": (self.mcp.resource_for(args.name) if hasattr(self.mcp, "resource_for")
                        else self.mcp.resource) + "#" + args.name,
                "title": "MCP: " + args.name,
                "text": text,
                "excerpt": text[:800],
                "kind": "mcp",
                "tool": args.name,
            }
            sources[sid] = source
            return source
        if name == "search_web":
            results = []
            for item in (await self.providers.search(args.query))[:3]:
                url = item.get("url", "")
                parsed = urlsplit(url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                    continue
                existing = next((s for s in sources.values() if s["url"] == url), None)
                if existing:
                    results.append({k: existing[k] for k in ("id", "url", "title", "excerpt")})
                    continue
                if len(sources) >= 24:
                    break
                sid = f"S{len(sources) + 1}"
                text = str(item.get("raw_content") or item.get("content") or "")[:8000]
                source = {
                    "id": sid,
                    "url": url,
                    "title": str(item.get("title", ""))[:300],
                    "text": text,
                    "excerpt": text[:800],
                }
                sources[sid] = source
                results.append({k: source[k] for k in ("id", "url", "title", "excerpt")})
            return results
        if name == "inspect_source":
            return sources[args.source_id]
        if name == "verify_quote":
            return {"present": args.quote in sources[args.source_id]["text"]}
        if name == "finish":
            # Check everything before committing any final output.
            for claim in args.claims:
                for evidence in claim.evidence:
                    if (
                        not evidence.quote.strip()
                        or evidence.quote not in sources[evidence.source_id]["text"]
                    ):
                        raise ValueError("Quote is absent")
            state["claims"] = [c.model_dump() for c in args.claims]
            state["answer"] = "\n\n".join(
                c.text + " " + " ".join(f"[{e.source_id}]" for e in c.evidence) for c in args.claims
            )
            state["limitations"] = args.limitations
            state["status"] = "completed"
            return {"completed": True}
        raise ValueError("Unknown tool")
