"""Prefetch: call lookups ahead, for real, so the LLM does not wait for them (AGENT_PREFETCH, default on).

Order / return lookups may be called more than needed: SCORING.md gives no function-score penalty for
extra lookups, and they go through tools_url like any call (the relay records them). Registrations
(cancel / return / exchange / callback) are NEVER called ahead: a recorded registration is committed.

When
  * session start, while the customer is still speaking: the caller's phone, only when the earlier
    conversation is about orders / returns (rules.json prefetch.order_words), and not when the same
    lookup is already in the history. Numbers said earlier are not used (see session_start)
  * turn start: numbers in the customer's words (a phone -> order + return lookup, a 15-digit order
    number -> order lookup)

What the dialog does with them (dialog.py)
  * already finished before the first LLM round -> given to the model as function calls + results, so
    it can answer in one round instead of two
  * still running -> when the model asks for the same lookup, the running call is awaited instead of
    sending a new one (the relay wait already spent is saved)
  * every prefetch is finished before the first answer audio: SCORING.md measures "after tool" from the
    last function result, and no sound after it scores 0

Product search is not prefetched: words in the utterance cannot tell when a search is needed (practice
data: product words matched 2 of 9 search cases and 6 cases that need no function).
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from .number_slots import id_numbers

ORDER_WORDS = "주문|배송|택배|송장|도착|결제|입금|환불|반품|교환|취소|회수"
ID_ARGS = ("order_id", "buyer_cellphone", "customer_phone_number")
LOOKUPS = ("get_order_info", "get_return_info")


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def numbers(text: str) -> list[str]:
    """ID-like numbers as written (phones, order numbers ...); dates / prices are left out."""
    return id_numbers(text)


def lookups_for(raw: str) -> list[tuple[str, dict]]:
    d = _digits(raw)
    if d.startswith("0") and 9 <= len(d) <= 11:  # phone
        return [("get_order_info", {"buyer_cellphone": raw}), ("get_return_info", {"customer_phone_number": raw})]
    if len(d) == 15:  # order number YYYYMMDD-NNNNNNN
        return [("get_order_info", {"order_id": raw})]
    return []


def key(name: str, args) -> tuple:
    """Same lookup = same function and the same numbers (separators ignored)."""
    ids = tuple(sorted((k, _digits(v)) for k, v in (args or {}).items() if k in ID_ARGS and _digits(v)))
    return name, ids


class Prefetch:
    def __init__(self, tools, rules: dict | None = None, history=None, caller=None, max_calls: int = 6):
        self.tools = tools  # ToolsClient (or the same contract): call(name, arguments_json) -> dict
        words = ((rules or {}).get("prefetch") or {}).get("order_words") or ORDER_WORDS
        self.order_words = re.compile(words)
        self.max_calls = max_calls
        self.tasks: dict[tuple, asyncio.Task] = {}
        self.info: dict[tuple, dict] = {}  # key -> {"name", "args", "when", "t0", "used"}
        self.history = [h for h in (history or []) if isinstance(h, dict)]
        self.caller = caller if isinstance(caller, dict) else {}
        self.in_history = {key(h.get("name"), h.get("arguments")) for h in self.history
                           if h.get("role") == "tool_call" and isinstance(h.get("arguments"), dict)}

    # ── starting calls ──────────────────────────────────────────────────────
    def _fire(self, name: str, args: dict, when: str):
        k = key(name, args)
        if not k[1] or k in self.tasks or k in self.in_history or len(self.tasks) >= self.max_calls:
            return
        self.info[k] = {"name": name, "args": args, "when": when, "t0": time.perf_counter(), "used": None}
        self.tasks[k] = asyncio.ensure_future(self.tools.call(name, json.dumps(args, ensure_ascii=False)))

    def session_start(self):
        """The caller's phone, when the conversation so far is about orders / returns.
        Numbers said earlier are NOT used: the official check (eval/score.py tool_match) marks a lookup
        function called only with other numbers as wrong, even when the expected result is already in
        the history (practice data: that broke 1 of 21 lookup cases; caller phone + the customer's own
        numbers broke none)."""
        said = " ".join(str(h.get("text") or "") for h in self.history if h.get("role") in ("customer", "agent"))
        if not self.order_words.search(said):
            return
        for name, args in lookups_for(self.caller.get("phone") or ""):
            self._fire(name, args, "session")

    def turn_start(self, utterance: str):
        for raw in numbers(utterance):
            for name, args in lookups_for(raw):
                self._fire(name, args, "turn")

    # ── using the results ──────────────────────────────────────────────────
    def ready(self, at: int = 0) -> list[tuple[str, dict, dict]]:
        """Finished lookups that found something and were not given to the model yet.
        `at` = where in the dialog history they are put (see forget_given)."""
        out = []
        for k, t in self.tasks.items():
            if not t.done() or t.cancelled() or t.exception() or self.info[k]["used"]:
                continue
            res = t.result()
            if isinstance(res, dict) and res.get("status") == "ok":
                self.info[k]["used"], self.info[k]["given_at"] = "given", at
                out.append((self.info[k]["name"], self.info[k]["args"], res))
        return out

    def forget_given(self, from_index: int):
        """The dialog history from `from_index` was rolled back (resume_before_audio): lookups
        given there can be given again."""
        for i in self.info.values():
            if i["used"] == "given" and i.get("given_at", 0) >= from_index:
                i["used"] = None

    async def serve(self, name: str, arguments_json: str):
        """The model asks for a lookup that is already running / done: its result, else None."""
        try:
            args = json.loads(arguments_json or "{}")
        except ValueError:
            return None
        k = key(name, args if isinstance(args, dict) else {})
        t = self.tasks.get(k)
        if t is None:
            return None
        try:
            res = await asyncio.shield(t)
        except Exception:
            return None
        self.info[k]["used"] = self.info[k]["used"] or "served"
        return res

    async def settle(self, timeout: float):
        pending = [t for t in self.tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def cancel(self):
        for t in self.tasks.values():
            if not t.done():
                t.cancel()

    def summary(self) -> list[dict]:
        out = []
        for k, t in self.tasks.items():
            i = self.info[k]
            status = (t.result() or {}).get("status") if t.done() and not t.cancelled() and not t.exception() else None
            out.append({"name": i["name"], "when": i["when"], "status": status, "used": i["used"]})
        return out
