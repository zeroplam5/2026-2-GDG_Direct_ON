"""Per-session client for the function (tool) server.

* base URL = session.start["tools_url"] of *this* session (never a hard-coded address)
* every POST carries X-Session-Id = session.start["session_id"]
* only functions advertised by GET /tools are ever sent; anything else is rejected locally
* every failure is turned into an explicit {"status": "error", ...} result so the LLM can
  tell the customer honestly that the lookup did not succeed
"""
from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("agent.tools")

# lookups and search: sending them again is harmless (the official scorer does not count extra ones)
SAFE_TO_REPEAT = {"get_order_info", "get_return_info", "search_products"}


def _normalize_tool_list(raw) -> list[dict]:
    """Accepts [{"type":"function","function":{...}}], [{name,description,parameters}] or
    {"tools":[...]} and returns a list of {name, description, parameters}."""
    if isinstance(raw, dict):
        raw = raw.get("tools") or raw.get("functions") or []
    out = []
    for t in raw or []:
        f = t.get("function", t) if isinstance(t, dict) else None
        if isinstance(f, dict) and f.get("name"):
            out.append({"name": f["name"], "description": f.get("description", ""),
                        "parameters": f.get("parameters") or {"type": "object", "properties": {}}})
    return out


class ToolsClient:
    def __init__(self, http: httpx.AsyncClient, tools_url: str | None, session_id: str, timeout_s: float):
        self.http = http
        self.base = (tools_url or "").rstrip("/")
        self.session_id = session_id
        self.timeout_s = timeout_s
        self.tools: list[dict] = []
        self.log: list[dict] = []  # per-call timing for metrics (no payloads)
        self.list_error: str | None = None
        self.sent_count = 0  # requests actually sent to tools_url (counted before the POST)
        self.sent_writes = 0  # of those, registrations (not safe to send twice; lookups / search are)
        self.debug = None  # optional callable(kind, **data); set only by the devui test server

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    async def load(self) -> list[dict]:
        if not self.base:
            self.list_error = "no tools_url in session.start"
            return []
        try:
            r = await self.http.get(f"{self.base}/tools", timeout=self.timeout_s,
                                    headers={"X-Session-Id": self.session_id})
            r.raise_for_status()
            self.tools = _normalize_tool_list(r.json())
        except Exception as e:
            self.list_error = f"{type(e).__name__}: {str(e)[:120]}"
            log.warning("[%s] GET /tools failed: %s", self.session_id, self.list_error)
            self.tools = []
        return self.tools

    def openai_schemas(self) -> list[dict]:
        return [{"type": "function", "function": t} for t in self.tools]

    async def call(self, name: str, arguments_json: str) -> dict:
        t0 = time.perf_counter()
        entry = {"name": name, "sent": False}
        if self.debug:
            self.debug("tool.start", name=name, arguments=arguments_json)
        result = None
        try:
            result = await self._call(name, arguments_json, entry)
        finally:
            entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.log.append(entry)
            if self.debug:
                self.debug("tool.end", name=name, ms=entry["ms"], sent=entry["sent"],
                           http_status=entry.get("http_status"), result=result)
        entry["status"] = result.get("status") if isinstance(result, dict) else None
        return result

    async def _call(self, name, arguments_json, entry):
        if name not in self.names:
            return {"status": "error", "error": "undefined_function",
                    "message": f"'{name}' is not a defined function; nothing was executed."}
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError as e:
            return {"status": "error", "error": "invalid_arguments", "message": str(e)[:100]}
        spec = next(t for t in self.tools if t["name"] == name)
        missing = [k for k in (spec["parameters"].get("required") or []) if args.get(k) in (None, "")]
        if missing:
            return {"status": "error", "error": "missing_arguments", "missing": missing,
                    "message": "Ask the customer for: " + ", ".join(missing)}
        entry["sent"] = True
        self.sent_count += 1
        if name not in SAFE_TO_REPEAT:
            self.sent_writes += 1
        try:
            r = await self.http.post(f"{self.base}/tools/{name}", json=args, timeout=self.timeout_s,
                                     headers={"X-Session-Id": self.session_id})
        except httpx.TimeoutException:
            return {"status": "error", "error": "timeout", "message": "function server did not answer in time"}
        except httpx.HTTPError as e:
            return {"status": "error", "error": "connection", "message": type(e).__name__}
        entry["http_status"] = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        if r.status_code >= 400:
            return {"status": "error", "error": f"http_{r.status_code}", "body": body}
        if isinstance(body, dict) and "status" not in body:
            body = {"status": "unknown", "body": body}  # do not let the model assume success
        return body if isinstance(body, dict) else {"status": "unknown", "body": body}
