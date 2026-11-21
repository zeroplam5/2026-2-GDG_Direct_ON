"""LLM <-> function-calling loop for one session.

run_turn() is an async generator of speakable sentences. Text is streamed sentence by
sentence so TTS can start before the LLM finishes. When the model asks for functions they
are checked by the pre-execution gate (gate.py), executed through the session's ToolsClient
and the results are fed back; the last allowed round forces tool_choice="none" so the turn
always ends with words.

When the gate blocks every call of a round:
  * the model already asked a question in that round  -> the turn ends there, so the
    customer does not hear the same question twice
  * the function has a confirm_template in the rules  -> that question is said directly
    (no second model call: faster, no gate wording leaking into the reply)
  * otherwise                                          -> the model gets "not_executed" and
    words the reply itself
Every model sentence passes the output check (output_guard.py) before it is yielded.
The system message is static instructions first and per-call context last (prompt cache).
"""
from __future__ import annotations

import asyncio
import json
import time

from . import policy
from .number_slots import NumberSlots
from .prefetch import LOOKUPS
from .output_guard import OutputGuard
from .textutil import SentenceSplitter

TEMPLATE_REASONS = {"no_consent", "not_asked", "not_stated"}


class Ack(str):
    """A short phrase said while functions run ("확인해 보겠습니다."). The session plays it from
    the pre-synthesized cache and does not count it as the turn's answer, so timeout/error
    fallbacks are still spoken after it."""


def ack_phrases(phrases: dict) -> list[str]:
    v = phrases.get("tool_ack") or []
    return [v] if isinstance(v, str) else [p for p in v if isinstance(p, str) and p.strip()]


def plan_blocked_reply(text: str, calls, decisions, gate, rules: dict) -> tuple[str, str | None]:
    """What to say after the gate blocked every call of a round.
    Returns ("already_asked", None) | ("template", sentence) | ("model", None)."""
    if text.strip() and gate.question.search(text):
        return "already_asked", None
    if len(calls) == 1 and decisions[0].reason in TEMPLATE_REASONS:
        tpl = ((rules.get("functions") or {}).get(calls[0].name) or {}).get("confirm_template")
        if tpl:
            try:
                return "template", tpl.format(**decisions[0].args)
            except (KeyError, IndexError, ValueError):
                pass
    return "model", None


def _gate_note(msg: dict) -> bool:
    """A blocked call's "not_executed" note: it quotes the rejected value, so it must never
    count as the value having been said or found (gate grounding)."""
    return msg.get("role") == "tool" and '"not_executed"' in str(msg.get("content"))


class Dialog:
    def __init__(self, llm, tools, system_prompt: str, cfg, phrases: dict, gate=None, context: str | None = None):
        self.llm = llm
        self.tools = tools
        self.cfg = cfg
        self.phrases = phrases
        self.gate = gate  # gate.Gate or None: code checks before any function call is sent
        self.rules = policy.load_rules(getattr(cfg, "rules_path", ""))  # hand-off signals, templates
        self.guard = OutputGuard(getattr(cfg, "output_guard", "log"), phrases.get("unverified_fallback", ""),
                                 strict=getattr(cfg, "output_guard_strict", ""), phrases=phrases, rules=self.rules)
        # One system message: static instructions first (same for every call, so the provider's
        # prompt cache can reuse it), per-call context (now, caller, history) at the end.
        # (A separate second system message was tried on 2026-11-19; the model then wrote its
        # reply twice in 5 of 6 text answers. Doubling also happens with one message, less
        # clearly measured, so repeated sentences are dropped by the output check as well.)
        content = system_prompt + ("\n\n" + context if context else "")
        self.static = system_prompt  # our instructions + brand material: what policy states (output check)
        # the customer's words in the session.start history ("고객: …" lines of the context)
        self.customer_history = "\n".join(ln[3:] for ln in (context or "").splitlines() if ln.startswith("고객:"))
        self.messages: list[dict] = [{"role": "system", "content": content}]
        # KULLM masks phone / order numbers in user messages: send them as slot names (number_slots.py).
        # self.messages always keeps the real values.
        self.slots = NumberSlots() if getattr(cfg, "number_slots", True) else None
        self.prefetch = None  # prefetch.Prefetch, set by session.py: order / return lookups called ahead
        self._acks = ack_phrases(phrases)
        self._ack_i = 0  # rotate so the same phrase is not repeated turn after turn
        self.debug = None  # optional callable(kind, **data); set only by the devui test server

    def _dbg(self, kind, **data):
        if self.debug:
            self.debug(kind, **data)

    def _last_agent_text(self, before: int) -> str | None:
        """What the agent said last before this turn: an earlier turn of this session, else
        None so the gate falls back to the session.start history."""
        for msg in reversed(self.messages[:before]):
            if msg["role"] == "assistant" and (msg.get("content") or "").strip():
                return msg["content"]
        return None

    def _sources(self) -> str:
        """Everything the model was given or told this session (for the output check)."""
        return "\n".join(str(msg.get("content") or "") for msg in self.messages)

    def _results(self) -> str:
        """Function results of this session only (what was actually done / found)."""
        return "\n".join(str(msg.get("content") or "") for msg in self.messages if msg["role"] == "tool")

    def _prior(self, turn_start: int) -> str:
        """Earlier turns of this session (customer words, replies, function results). Empty for
        the evaluator, which sends one utterance per connection with the history in session.start."""
        return "\n".join(str(msg.get("content") or "") for msg in self.messages[1:turn_start] if not _gate_note(msg))

    def _turn_results(self, turn_start: int) -> str:
        """Function results of the current turn so far."""
        return "\n".join(str(msg.get("content") or "") for msg in self.messages[turn_start:]
                         if msg["role"] == "tool" and not _gate_note(msg))

    def _customer_words(self) -> str:
        """Everything the customer said: session.start history + this session's turns."""
        return "\n".join([self.customer_history] + [str(msg.get("content") or "") for msg in self.messages
                                                    if msg["role"] == "user"])

    def _outgoing(self) -> list[dict]:
        """What is sent to the provider (slot names for numbers in user messages)."""
        return self.slots.outgoing(self.messages) if self.slots else self.messages

    def _real(self, text):
        """Slot names the model wrote -> real values."""
        return self.slots.decode(text) if self.slots else text

    async def _settle(self, m: dict):
        """Before the first answer audio: wait until every prefetched lookup has its result
        (the evaluator measures "after tool" from the last function result; no sound after it = 0)."""
        if self.prefetch and not m.get("_prefetch_settled"):
            m["_prefetch_settled"] = True
            t = time.perf_counter()
            await self.prefetch.settle(getattr(self.cfg, "prefetch_settle_s", 2.0))
            waited = round((time.perf_counter() - t) * 1000, 1)
            if waited >= 1:
                m["prefetch_wait_ms"] = waited

    def _give_prefetched(self, turn_start: int):
        """Lookups that finished while the customer was speaking go in as function calls + results
        before the first LLM round, so the model can answer in one round."""
        given = self.prefetch.ready() if self.prefetch else []
        if not given:
            return []
        ids = [f"prefetch_{turn_start}_{i}" for i in range(len(given))]
        self.messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
            for i, (n, a, _) in zip(ids, given)]})
        for i, (_, _, r) in zip(ids, given):
            self.messages.append({"role": "tool", "tool_call_id": i, "content": json.dumps(r, ensure_ascii=False)})
        return [n for n, _, _ in given]

    def _checked(self, sentence: str, m: dict, said: list[str]) -> str:
        """Output check; returns "" when the sentence must not be said (e.g. already said)."""
        sentence = self._real(sentence)
        out, flag = self.guard.check(sentence, self._sources(), said, self._results(), policy=self.static,
                                     customer=self._customer_words())
        if flag:
            m.setdefault("output_flags", []).append(flag)
            self._dbg("output_guard", sentence=sentence, flag=flag)
        if out:
            said.append(out)
        return out

    async def run_turn(self, user_text: str, m: dict):
        flags = policy.handoff_flags(user_text, self.rules)
        m["handoff_flags"] = flags
        turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": policy.user_message(user_text, flags, self.rules)})
        if self.prefetch:
            self.prefetch.turn_start(user_text)  # numbers in the customer's words: lookups start now
            given = self._give_prefetched(turn_start)
            if given:
                m["prefetch_given"] = given
                self._dbg("prefetch.given", names=given)
        schemas = self.tools.openai_schemas()
        if self.gate:
            schemas = self.gate.augment(schemas)
            m.setdefault("gate", [])
        m.setdefault("llm_rounds", 0)
        m.setdefault("tool_calls", [])
        spoke = False
        said: list[str] = []  # sentences actually said this turn (after the output check)
        for rnd in range(self.cfg.max_tool_rounds + 1):
            last_round = rnd == self.cfg.max_tool_rounds
            splitter = SentenceSplitter(early_min_chars=0 if spoke else self.cfg.first_chunk_min_chars)
            text, calls = "", None
            t_req = time.perf_counter()
            m["llm_rounds"] += 1
            self._dbg("llm.round_start", round=rnd)
            async for kind, val in self.llm.stream(self._outgoing(), tools=schemas or None,
                                                   tool_choice="none" if last_round else "auto"):
                if kind == "text":
                    if "t_llm_first_text" not in m:
                        m["t_llm_first_text"] = time.perf_counter()
                        m["llm_first_text_round_ms"] = round((m["t_llm_first_text"] - t_req) * 1000, 1)
                        self._dbg("llm.first_text", round=rnd)
                    text += val
                    for s in splitter.push(val):
                        out = self._checked(s, m, said)
                        if out:
                            await self._settle(m)
                            spoke = True
                            yield out
                elif kind == "tool_calls" and not last_round:
                    calls = val
            for s in splitter.flush():
                out = self._checked(s, m, said)
                if out:
                    await self._settle(m)
                    spoke = True
                    yield out
            text = self._real(text)
            for c in calls or []:
                c.arguments = self._real(c.arguments)  # the gate and the function server get real values
            self._dbg("llm.round_end", round=rnd, text=text,
                      tool_calls=[{"name": c.name, "arguments": c.arguments} for c in calls or []])
            if not calls:
                if not text.strip():  # model returned nothing speakable
                    text = self.phrases["tool_round_limit"]
                    await self._settle(m)
                    yield text
                    said.append(text)
                # history keeps what the customer actually heard this turn
                self.messages.append({"role": "assistant", "content": " ".join(said) or text})
                return
            self.messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]})
            decisions = [None] * len(calls)
            if self.gate:
                last_agent = self._last_agent_text(turn_start)
                prior, fresh = self._prior(turn_start), self._turn_results(turn_start)
                decisions = [self.gate.check(c.name, c.arguments, user_text, last_agent, prior=prior, turn_results=fresh)
                             for c in calls]
                m["gate"].extend(d.log_entry(c.name) for c, d in zip(calls, decisions))
                self._dbg("gate", decisions=[{"name": c.name, "allowed": d.allowed, "reason": d.reason,
                                              "args": d.args, "message": d.message}
                                             for c, d in zip(calls, decisions)])
            will_send = any(d is None or d.allowed for d in decisions)
            if will_send and not spoke and self.cfg.tool_ack and self._acks:
                ack = self._acks[self._ack_i % len(self._acks)]
                self._ack_i += 1
                m["tool_ack"] = ack
                spoke = True
                self._dbg("tool_ack", text=ack)
                yield Ack(ack)  # the session plays it while the functions below run
            t_tools = time.perf_counter()

            async def run(c, d):
                if d is not None and not d.allowed:
                    return d.result()  # blocked: nothing is sent to tools_url
                args_json = c.arguments if d is None else json.dumps(d.args, ensure_ascii=False)
                if self.prefetch and c.name in LOOKUPS:  # already called ahead: wait for that call
                    got = await self.prefetch.serve(c.name, args_json)
                    if got is not None:
                        return got
                return await self.tools.call(c.name, args_json)

            results = await asyncio.gather(*(run(c, d) for c, d in zip(calls, decisions)))
            m["tool_calls"].append({"round": rnd, "ms": round((time.perf_counter() - t_tools) * 1000, 1),
                                    "calls": [{"name": c.name, "status": r.get("status")} for c, r in zip(calls, results)]})
            for c, r in zip(calls, results):
                self.messages.append({"role": "tool", "tool_call_id": c.id,
                                      "content": json.dumps(r, ensure_ascii=False)})

            if self.gate and not will_send:  # every call of this round was blocked
                how, sentence = plan_blocked_reply(text, calls, decisions, self.gate, self.rules)
                m["blocked_reply"] = how
                self._dbg("blocked_reply", how=how, sentence=sentence)
                if how == "already_asked":  # the question was just said: do not repeat it
                    return
                if how == "template":
                    self.messages.append({"role": "assistant", "content": sentence})
                    await self._settle(m)
                    yield sentence
                    return
                # "model": the model sees not_executed and words the reply in the next round
        await self._settle(m)
        yield self.phrases["tool_round_limit"]
