"""Official practice cases as TEXT through the agent's LLM loop (no audio, no STT / TTS).

Each case's customer utterance goes straight into the real Dialog (prompt + brand material,
pre-execution gate, output check) with the case's history / caller / now, exactly as the
session would build them. Functions are answered from the kit's data (reference results and
data/mock_db.json, in-process), and the function score uses the same rule as the kit's
eval/score.py tool_match(). Nothing from the kit is copied into the repository: point --kit at
the unpacked day kit (the folder that has data/practice_cases.jsonl and data/brand/).

  # no LLM, no cost: replay the reference calls through the gate (are correct calls let through?)
  python -m team.scripts.llm_cases --kit <day kit dir> --gate-only
  # real LLM with the .env settings (model, key); 4 cases at a time
  python -m team.scripts.llm_cases --kit <day kit dir> --limit 20
  python -m team.scripts.llm_cases --kit <day kit dir> --only <case_id>,<case_id> --show

Output stays on the screen. --out writes a JSONL with the replies (contains kit text: keep it
out of git, e.g. under team/runs/llm_cases/ which is ignored).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import re
import sys
import time
from pathlib import Path

from team.agent import policy
from team.agent.config import REPO_ROOT, Config
from team.agent.dialog import Ack, Dialog
from team.agent.envfile import load_env_file
from team.agent.gate import Gate
from team.agent.prefetch import Prefetch
from team.agent.providers import make_llm

LOOKUP_FUNCS = {"get_order_info", "get_return_info"}
INFO_FUNCS = {"search_products"}
ID_ARGS = ["order_id", "buyer_cellphone", "customer_phone_number", "phone_number", "order_number"]


def digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def tool_match(ref_calls, got_calls, history=()):
    """Same rule as the kit's eval/score.py (2026-11-21)."""
    ref_calls = [c for c in ref_calls if c["name"] not in INFO_FUNCS]
    got_calls = [c for c in got_calls if c["name"] not in INFO_FUNCS]
    seen = {h.get("name") for h in history if h.get("role") == "tool_result"}
    ref_names = {c["name"] for c in ref_calls}
    extra = sorted({c["name"] for c in got_calls} - ref_names - LOOKUP_FUNCS)
    if extra:
        return False, f"부르지 말아야 할 접수 함수: {extra}"
    for rc in ref_calls:
        called = [c for c in got_calls if c["name"] == rc["name"]]
        if not called:
            if rc["name"] in LOOKUP_FUNCS and rc["name"] in seen:
                continue
            return False, f"{rc['name']} 호출 안 함"
        want = {k: digits(rc["arguments"].get(k)) for k in ID_ARGS if digits(rc["arguments"].get(k))}
        if want and not any(any(digits(c["arguments"].get(k)) == v for k, v in want.items()) for c in called):
            return False, f"{rc['name']} 번호 다름"
    return True, ""


RELAY_DELAY_S: dict = {}  # filled by --relay-delay


class KitTools:
    """Stands in for tools_url: same contract as ToolsClient (openai_schemas / call). Answers like
    the day kit's SPEC.md mock server: lookups by phone / order number ignoring separators
    (not_found otherwise), registrations ok / invalid by required values, search = closest record.
    The case's own reference result is used when the call matches it."""

    def __init__(self, tools: list[dict], db: dict, case: dict):
        self.tools = {t["name"]: t for t in tools}
        self.db = db
        self.ref = list(zip(case["reference"].get("tool_calls", []), case["reference"].get("tool_results", [])))
        self.sent: list[dict] = []  # what the evaluator's relay would record

    def openai_schemas(self):
        return [{"type": "function", "function": t} for t in self.tools.values()]

    @staticmethod
    def _ids(args) -> set[str]:
        return {digits(args.get(k)) for k in ID_ARGS if isinstance(args, dict) and digits(args.get(k))}

    async def call(self, name, arguments_json):
        try:
            args = json.loads(arguments_json or "{}")
        except ValueError:
            return {"status": "invalid"}
        if name not in self.tools:
            return {"status": "error", "message": "unknown function"}
        self.sent.append({"name": name, "arguments": args})
        if RELAY_DELAY_S:  # --relay-delay: answer after the evaluator relay's per-function delay
            await asyncio.sleep(RELAY_DELAY_S.get(name, RELAY_DELAY_S["_default"]))
        for rc, rr in self.ref:
            if rc["name"] == name and (self._ids(rc["arguments"]) & self._ids(args) or name in INFO_FUNCS
                                       or not self._ids(rc["arguments"])):
                # reference results are stored as {"name": ..., "result": {...}}; the server returns the inner part
                return rr.get("result", rr) if isinstance(rr, dict) and "name" in rr else rr
        if name in INFO_FUNCS:
            q = set(str(args.get("query") or ""))
            recs = self.db.get(name) or []
            best = max(recs, key=lambda r: len(q & set(json.dumps(r["arguments"], ensure_ascii=False))), default=None)
            return best["result"] if best else {"status": "not_found"}
        if name in LOOKUP_FUNCS:
            want = self._ids(args)
            for r in self.db.get(name) or []:
                if want & self._ids(r["arguments"]):
                    return r["result"]
            return {"status": "not_found"}
        required = (self.tools[name].get("parameters") or {}).get("required") or []
        return {"status": "ok"} if all(args.get(k) not in (None, "", []) for k in required) else {"status": "invalid"}


def load_kit(kit: Path):
    cases = [json.loads(line) for line in (kit / "data" / "practice_cases.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    tools = json.loads((kit / "data" / "brand" / "tools.json").read_text(encoding="utf-8"))
    db = json.loads((kit / "data" / "mock_db.json").read_text(encoding="utf-8"))
    return cases, tools, db


def gate_only(cases, tools, rules):
    schemas = [{"type": "function", "function": t} for t in tools]
    tally, blocked = collections.Counter(), []
    for c in cases:
        for tc in c["reference"].get("tool_calls", []):
            # evidence on: the reference calls carry no 근거 칸, so this checks the fallback path
            g = Gate(rules, {"history": c["history"], "caller": c.get("customer_profile")}, evidence=True)
            g.augment(schemas)
            d = g.check(tc["name"], json.dumps(tc["arguments"], ensure_ascii=False), c["customer_utterance"])
            tally[d.allowed] += 1
            if not d.allowed:
                blocked.append(f"  {c['case_id']} {tc['name']}: {d.reason}")
    print(f"정답 호출 {sum(tally.values())}개 중 관문 통과 {tally[True]}개, 차단 {tally[False]}개")
    print("\n".join(blocked))


async def run_case(c, cfg, llm, tools, db, static_template, phrases, rules):
    start = {"session_id": c["case_id"], "now": c["now"], "history": c["history"], "caller": c.get("customer_profile")}
    kt = KitTools(tools, db, c)
    dlg = Dialog(llm, kt, policy.build_static_prompt(static_template, phrases, rules, c["now"]), cfg, phrases,
                 gate=Gate(rules, start, evidence=cfg.gate_evidence) if cfg.tool_gate else None,
                 context=policy.build_context(start))
    if cfg.prefetch:  # as in the session: lookups called ahead while the customer is still speaking
        dlg.prefetch = Prefetch(kt, rules, c["history"], c.get("customer_profile"), max_calls=cfg.prefetch_max_calls)
        dlg.prefetch.session_start()
        await dlg.prefetch.settle(cfg.prefetch_settle_s)
    if cfg.greeting_first and not c["history"]:  # as in the session: the code greets first
        dlg.greeting = phrases.get("greeting")
    blocked = []  # gate decisions with their arguments (the session log keeps no payloads)
    dlg.debug = lambda kind, **d: blocked.extend(
        {"name": x["name"], "reason": x["reason"], "args": x["args"]} for x in d.get("decisions", []) if not x["allowed"]
    ) if kind == "gate" else None
    m, said, first_ms = {}, [], None
    t0 = time.perf_counter()
    try:
        async for s in dlg.run_turn(c["customer_utterance"], m):
            if first_ms is None and not isinstance(s, Ack):  # LLM time: the greeting / ack is not counted
                first_ms = (time.perf_counter() - t0) * 1000
            said.append(("(대기) " if isinstance(s, Ack) else "") + s)
        err = None
    except Exception as e:  # keep the batch going; the error is reported per case
        err = repr(e)
    ok, why = tool_match(c["reference"].get("tool_calls", []), kt.sent, c["history"])
    return {"case_id": c["case_id"], "tool_ok": ok, "why": why, "sent": kt.sent, "gate": m.get("gate") or [],
            "blocked": blocked,
            "flags": m.get("output_flags") or [], "reply": " ".join(said), "first_ms": first_ms, "error": err,
            "ref_calls": [t["name"] for t in c["reference"].get("tool_calls", [])]}


async def run_llm(cases, tools, db, args):
    cfg = Config.from_env()
    rules = policy.load_rules(cfg.rules_path)
    phrases = policy.load_phrases(cfg.phrases_path)
    brand = policy.load_brand(Path(args.kit) / "data" / "brand")
    template = policy.compose_prompt(policy.load_prompt(cfg.prompt_path), brand)
    llm = make_llm(cfg)
    print(f"모델 {cfg.llm_model} · 규칙 {Path(cfg.rules_path).parent.name} · 브랜드 자료 "
          f"{'있음' if brand else '없음'} · 문항 {len(cases)}개 · 동시 {args.concurrency}")
    sem = asyncio.Semaphore(args.concurrency)

    async def one(c):
        async with sem:
            return await run_case(c, cfg, llm, tools, db, template, phrases, rules)

    results = await asyncio.gather(*(one(c) for c in cases))
    blocked = collections.Counter()
    for r in results:
        for g in r["gate"]:
            if not g.get("allowed"):
                blocked[f"{g['name']}:{g['reason']}"] += 1
        mark = "✓" if r["tool_ok"] else "✗"
        calls = ", ".join(f"{s['name']}" for s in r["sent"]) or "-"
        ref = ", ".join(r["ref_calls"]) or "-"
        fm = f"{r['first_ms']:.0f}ms" if r["first_ms"] is not None else "-"
        print(f"{mark} {r['case_id']:10} 정답[{ref}] 보냄[{calls}] 첫문장 {fm} {r['why']}{' ERROR ' + r['error'] if r['error'] else ''}")
        if args.show:
            print("    답:", r["reply"][:300])
            for b in r["blocked"]:
                print("    관문 차단:", b)
            for f in r["flags"]:
                print("    출력 검사:", f)
    n = len(results)
    ok = sum(r["tool_ok"] for r in results)
    firsts = sorted(r["first_ms"] for r in results if r["first_ms"] is not None)
    print(f"\n함수 점수 {ok}/{n} = {100 * ok / max(n, 1):.1f}  (채점기와 같은 규칙, 함수 없는 문항 포함)")
    if firsts:
        print(f"첫 문장까지(LLM만, 음성 제외) 중앙값 {firsts[len(firsts) // 2]:.0f}ms")
    print("관문 차단:", dict(blocked) or "없음", "· 오류:", sum(1 for r in results if r["error"]))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kit", required=True, help="unpacked day kit folder (has data/practice_cases.jsonl)")
    ap.add_argument("--gate-only", action="store_true", help="no LLM: replay the reference calls through the gate")
    ap.add_argument("--only", default="", help="comma separated case ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--functions-only", action="store_true", help="only cases whose reference calls a function")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--show", action="store_true", help="print replies, gate blocks and output-check flags")
    ap.add_argument("--out", default="", help="JSONL with replies (kit text: keep out of git)")
    ap.add_argument("--env", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--relay-delay", action="store_true",
                    help="each function answers after the evaluator relay's delay (lookup 1.0-1.2 s, search 1.5, "
                         "callback 0.8, other registrations 1.5), so 첫문장 includes the waits")
    args = ap.parse_args()
    if args.relay_delay:
        RELAY_DELAY_S.update({"get_order_info": 1.2, "get_return_info": 1.0, "search_products": 1.5,
                              "request_callback": 0.8, "_default": 1.5})
    load_env_file(args.env)
    kit = Path(args.kit)
    cases, tools, db = load_kit(kit)
    if args.only:
        keep = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["case_id"] in keep]
    if args.functions_only:
        cases = [c for c in cases if c["reference"].get("tool_calls")]
    if args.limit:
        cases = cases[: args.limit]
    if args.gate_only:
        gate_only(cases, tools, policy.load_rules(Config.from_env().rules_path))
        return
    asyncio.run(run_llm(cases, tools, db, args))


if __name__ == "__main__":
    sys.exit(main())
