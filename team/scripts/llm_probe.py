"""LLM 확인용 출력기: 상담원과 같은 프롬프트·요청 경로·실행 전 관문으로 모델의 결정을 보고,
결과를 사람이 읽기 쉬운 표로 보여 준다. 키 값은 출력하지 않는다.

    .venv/Scripts/python -m team.scripts.llm_probe                        # 기본 발화
    .venv/Scripts/python -m team.scripts.llm_probe --case K07             # 문항 하나 (K01~K08, G01~G09)
    .venv/Scripts/python -m team.scripts.llm_probe --suite gate           # 관문 시험 문항 G01~G09 전체
    .venv/Scripts/python -m team.scripts.llm_probe --case G03 --no-gate   # 관문 없이 (개선 전 비교)
    .venv/Scripts/python -m team.scripts.llm_probe --say "네" --history '[{"role":"agent","text":"..."}]'
    .venv/Scripts/python -m team.scripts.llm_probe --effort none,low      # 추론 강도 비교
    .venv/Scripts/python -m team.scripts.llm_probe --case K07 --show-prompt   # 조립된 프롬프트도 출력

- 프롬프트: team/agent/policy.py의 조립 함수와 AGENT_PROMPT_PATH(기본: 연습용 지침)
- 요청: team/agent/providers/llm.py의 OpenAICompatLLM (상담원과 같은 파라미터)
- 관문: team/agent/gate.py와 AGENT_TOOL_POLICY_PATH (상담원과 같은 규칙표)
- 보는 범위: 모델의 첫 결정 → 관문 판정 → 막혔다면 모델이 고객에게 할 말까지.
  관문을 통과한 함수는 실제로 부르지 않는다(모의 서버 없이 확인).
- history 형식은 공식 규격이 아직 없다(11/21 공개). [{"role": "agent|customer", "text": "..."}] 모양을 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

from team.agent import policy
from team.agent.config import OFFICIAL_PACK, PRACTICE_PATHS, Config, repo_path
from team.agent.dialog import Dialog, plan_blocked_reply
from team.agent.envfile import load_env_file
from team.agent.gate import Gate
from team.agent.providers.llm import OpenAICompatLLM, model_params
from team.mock.mock_api import DEFAULT_DATA, DEFAULT_TOOLS, MockStore

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAY = "주문번호 DEMO1001 배송 어디쯤이에요?"
DEFAULT_NOW = "2026-11-21 11:00"
CASE_FILES = {"practice": ROOT / "team" / "data" / "practice_ko" / "cases.jsonl",
              "gate": ROOT / "team" / "data" / "gate_ko" / "cases.jsonl"}
REASON_KO = {"no_consent": "동의 없음", "not_asked": "직전에 묻지 않음", "hesitation": "거절·보류·정정",
             "puzzled": "되물음", "not_grounded": "대화에 없는 값", "invalid_format": "형식 오류",
             "invalid_arguments": "인자 오류", "not_stated": "값을 말하지 않음"}


class C:
    on = True

    @classmethod
    def w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else s


def bold(s): return C.w("1", s)
def dim(s): return C.w("2", s)
def green(s): return C.w("32", s)
def red(s): return C.w("31", s)
def cyan(s): return C.w("36", s)
def yellow(s): return C.w("33", s)


def width(s):
    """터미널 표시 폭: 한글 등 전각 문자는 2칸."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s, w, right=False):
    gap = " " * max(0, w - width(s))
    return gap + s if right else s + gap


def rule(title=""):
    if not title:
        return dim("─" * 64)
    return bold(f"── {title} ") + dim("─" * max(4, 60 - width(title)))


def load_tools():
    defs = json.loads((ROOT / "data" / "demo_tools.json").read_text(encoding="utf-8"))
    return [{"type": "function", "function": d} for d in defs]


def read_cases(kind):
    return [json.loads(l) for l in CASE_FILES[kind].read_text(encoding="utf-8").splitlines() if l.strip()]


def load_case(case_id):
    for kind in CASE_FILES:
        for c in read_cases(kind):
            if c.get("case_id") == case_id:
                return c
    raise SystemExit(f"{case_id} 문항이 없습니다. (K01~K08, G01~G09)")


def clean_utterance(s):
    """연습 문항 문장 속 쉼 표기("/ <pause 1.3s> /")를 지워 STT 결과처럼 만든다."""
    return re.sub(r"\s*/\s*<pause[^>]*>\s*/\s*", " ", s).strip()


def fmt_call(name, raw_args, drop_internal=True):
    try:
        args = json.loads(raw_args or "{}")
        if drop_internal:
            args = {k: v for k, v in args.items() if not k.startswith("_")}
        inner = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in args.items())
    except ValueError:
        inner = raw_args
    return f"{name}({inner})"


def short(text, n=60):
    text = " ".join(text.split())
    return text if width(text) <= n * 2 else text[:n] + "…"


def build(case, a):
    say = a.say or (clean_utterance(case["customer_utterance"]) if case else DEFAULT_SAY)
    start = {"now": a.now or (case or {}).get("now") or DEFAULT_NOW,
             "history": json.loads(a.history) if a.history else (case or {}).get("history") or [],
             "caller": json.loads(a.caller) if a.caller else (case or {}).get("customer_profile")}
    return say, start


def template_for(cfg):
    """Pack prompt; with the official pack the brand material is appended like the server does."""
    template = policy.load_prompt(cfg.prompt_path)
    if Path(cfg.rules_path).resolve().parent == OFFICIAL_PACK.resolve():
        template = policy.compose_prompt(template, policy.load_brand(repo_path(cfg.brand_dir)))
    return template


def messages_for(cfg, say, start):
    """Same messages the agent sends: static instructions, per-call context, customer turn."""
    template = template_for(cfg)
    phrases = policy.load_phrases(cfg.phrases_path)
    rules = policy.load_rules(cfg.rules_path)
    flags = policy.handoff_flags(say, rules)
    static = policy.build_static_prompt(template, phrases, rules, start.get("now"))
    return flags, [{"role": "system", "content": static + "\n\n" + policy.build_context(start)},
                   {"role": "user", "content": policy.user_message(say, flags, rules)}]


class LocalTools:
    """--full: stands in for the function server with the same contract as ToolsClient,
    answering from the team mock data in-process (team/mock/mock_api.MockStore)."""

    def __init__(self):
        self.store = MockStore(DEFAULT_TOOLS, DEFAULT_DATA)
        self.sent = []

    def openai_schemas(self):
        return [{"type": "function", "function": t} for t in self.store.tools]

    async def call(self, name, arguments_json):
        try:
            args = json.loads(arguments_json or "{}")
        except ValueError:
            return {"status": "error", "error": "invalid_arguments"}
        if name not in self.store.tool_names:
            return {"status": "error", "error": "undefined_function"}
        result, _ = self.store.lookup(name, args)
        self.sent.append((name, args, result.get("status")))
        return result


async def run_full(cfg, llm, say, start, gate_on):
    """The real Dialog loop (gate, blocked-reply paths, output check) with mock function results."""
    tools = LocalTools()
    phrases = policy.load_phrases(cfg.phrases_path)
    rules = policy.load_rules(cfg.rules_path)
    dlg = Dialog(llm, tools, policy.build_static_prompt(template_for(cfg), phrases, rules,
                                                       start.get("now")),
                 cfg, phrases, gate=Gate(rules, start) if gate_on else None, context=policy.build_context(start))
    m, said, t0, first = {}, [], time.perf_counter(), None
    async for s in dlg.run_turn(say, m):
        first = first if first is not None else time.perf_counter() - t0
        said.append(str(s))
    return {"first": first, "total": time.perf_counter() - t0, "said": said, "sent": tools.sent,
            "gate": m.get("gate", []), "blocked_reply": m.get("blocked_reply"),
            "output_flags": m.get("output_flags", []), "rounds": m.get("llm_rounds")}


def outcome_full(r):
    if r.get("error"):
        return red("오류 " + r["error"])
    parts = []
    for g in r["gate"]:
        if not g["allowed"]:
            parts.append(yellow(f"관문 차단 {g['name']}({REASON_KO.get(g['reason'], g['reason'])})"))
    for name, args, status in r["sent"]:
        inner = ", ".join(f'{k}="{v}"' for k, v in args.items())
        parts.append(cyan(f"함수 {name}({inner}) → 결과 {status}"))
    for s in r["said"]:
        parts.append(f'말 "{short(s, 70)}"')
    note = {"template": "정해진 확인 문장으로 답함", "already_asked": "이미 질문해서 추가로 말하지 않음",
            "model": "모델이 다시 답함"}.get(r.get("blocked_reply"))
    if note:
        parts.append(dim(f"({note}, 모델 호출 {r['rounds']}회)"))
    repeats = sum(1 for f in r["output_flags"] if f.get("kind") == "repeated_sentence")
    if repeats:
        parts.append(yellow(f"출력 검사: 모델이 반복한 문장 {repeats}개를 말하지 않음"))
    for f in r["output_flags"]:
        if f.get("kind") == "ungrounded_number":
            parts.append(red(f"출력 검사: 근거 없는 숫자 {', '.join(f['values'])} ({f['action']})"))
    return "\n          ".join(parts) or yellow("빈 응답")


def show_prompt(messages, tools):
    print()
    print(rule("조립된 프롬프트"))
    for i, m in enumerate(messages):
        label = {"system": "system (앞: 지침·함수 규칙, 매 통화 같음 / 끝: 상담 정보, 매 통화 다름)",
                 "user": "user (고객 발화)"}.get(m["role"], m["role"])
        print(cyan(f" [{label}]"))
        for line in m["content"].splitlines():
            print("   " + line)
        print()
    print(cyan(" [tools]") + dim("  모델에 함께 보내는 함수 정의"))
    for t in tools:
        f = t["function"]
        req = f.get("parameters", {}).get("required", [])
        print(f"   {f['name']}({', '.join(req)})  " + dim(f.get("description", "")))


async def stream_once(llm, messages, tools):
    t0 = time.perf_counter()
    first, text, calls = None, "", []
    async for kind, payload in llm.stream(messages, tools=tools):
        if first is None:
            first = time.perf_counter() - t0
        if kind == "text":
            text += payload
        elif kind == "tool_calls":
            calls = payload
    return first, time.perf_counter() - t0, text.strip(), calls


async def decide(llm, messages, tools, gate, say):
    """First model decision -> gate -> if every call was blocked, what the model says next."""
    r = {"text": "", "calls": [], "decisions": [], "reply": None, "t_reply": None}
    r["first"], r["total"], r["text"], calls = await stream_once(llm, messages, tools)
    r["calls"] = [(c.name, c.arguments) for c in calls]
    if not calls or not gate:
        return r
    r["decisions"] = [gate.check(c.name, c.arguments, say) for c in calls]
    if all(not d.allowed for d in r["decisions"]):
        how, sentence = plan_blocked_reply(r["text"], calls, r["decisions"], gate, gate.table)
        r["blocked_reply"] = how
        if how == "template":
            r["reply"], r["t_reply"] = sentence, 0.0
            return r
        if how == "already_asked":
            return r
        follow = messages + [{"role": "assistant", "content": r["text"] or None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]}]
        follow += [{"role": "tool", "tool_call_id": c.id, "content": json.dumps(d.result(), ensure_ascii=False)}
                   for c, d in zip(calls, r["decisions"])]
        _, r["t_reply"], r["reply"], again = await stream_once(llm, follow, tools)
        if again and not r["reply"]:
            r["reply"] = "(다시 함수 호출: " + "; ".join(fmt_call(c.name, c.arguments) for c in again) + ")"
    return r


def executed(r):
    """Would a function call actually be sent to tools_url in this run?"""
    if r.get("error"):
        return False
    if "sent" in r:  # --full: what was really sent to the (mock) function server
        return bool(r["sent"])
    if not r["calls"]:
        return False
    if not r["decisions"]:
        return True  # no gate
    return any(d.allowed for d in r["decisions"])


def attempted(r):
    """Did the model try to call a function (whether or not the gate let it through)?"""
    return bool(r.get("gate") or r.get("sent")) if "sent" in r else bool(r.get("calls"))


def outcome(r):
    if r.get("error"):
        return red("오류 " + r["error"])
    parts = []
    if r["text"]:
        parts.append('말 "' + short(r["text"]) + '"')
    if r["calls"]:
        parts.append(cyan("함수 " + "; ".join(fmt_call(n, a) for n, a in r["calls"])))
    for d in r["decisions"]:
        parts.append(green("→ 관문 통과, 전송") if d.allowed else
                     yellow(f"→ 관문 차단({REASON_KO.get(d.reason, d.reason)})"))
    if r.get("blocked_reply") == "already_asked":
        parts.append(dim("→ 이미 질문해서 추가로 말하지 않음"))
    if r["reply"] is not None:
        how = dim(" (정해진 확인 문장)") if r.get("blocked_reply") == "template" else dim(f" (+{r['t_reply']:.2f}s)")
        parts.append(f'→ 말 "{short(r["reply"])}"' + how)
    return "\n          ".join(parts) or yellow("빈 응답")


def secs(x):
    return f"{x:5.2f}s" if isinstance(x, (int, float)) else "   - "


async def run_one(a, cfg, llm, messages, tools, gate, say, start):
    try:
        if a.full:
            return await run_full(cfg, llm, say, start, gate is not None)
        return await decide(llm, messages, tools, gate, say)
    except Exception as e:  # 키 값은 예외 메시지에 들어가지 않는다
        return {"first": None, "total": None, "text": "", "calls": [], "decisions": [], "reply": None,
                "t_reply": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def show(r):
    return outcome_full(r) if "sent" in r else outcome(r)


async def single(a, base, efforts, case, tools_raw):
    say, start = build(case, a)
    gate = None if a.no_gate else Gate(policy.load_rules(base.rules_path), start)
    tools = gate.augment(tools_raw) if gate else tools_raw
    flags, messages = messages_for(base, say, start)

    print(f" {bold('문항')}    " + ((a.case + "  " + case.get("label", "")) if a.case else "직접 입력")
          + dim(f"  now={start['now']}"))
    if start["caller"]:
        print(f" {bold('발신자')}  " + json.dumps(start["caller"], ensure_ascii=False))
    if start["history"]:
        print(f" {bold('이전 대화')}")
        for line in policy.format_history(start["history"]).splitlines():
            print("   " + dim(line))
    print(f" {bold('발화')}    \"{say}\"")
    if flags:
        print(f" {bold('인계 신호')} " + yellow(", ".join(flags)))
    if case and case.get("expect"):
        e = case["expect"]
        print(f" {bold('기대')}    " + ("함수 실행" if e["execute"] else "실행 안 함") + f" · 응답: {e['reply']}")
    print(f" {bold('관문')}    " + (dim("꺼짐 (--no-gate)") if not gate else "켜짐")
          + dim("   모드: " + ("끝까지 (모의 데이터로 함수 결과 뒤 답까지)" if a.full else "첫 결정")))
    if a.show_prompt:
        show_prompt(messages, tools)

    rows = []
    for eff in efforts:
        cfg = dataclasses.replace(base, llm_reasoning_effort=eff)
        print()
        print(rule(f"effort={eff}"))
        print(" " + dim("보내는 값  " + ", ".join(f"{k}={v}" for k, v in model_params(cfg).items())))
        print(" " + dim(pad("회차", 5) + pad("첫 소리" if a.full else "첫 결정", 9, True) + "   결과"))
        llm = OpenAICompatLLM(cfg)
        for i in range(1, a.repeat + 1):
            r = await run_one(a, cfg, llm, messages, tools, gate, say, start)
            r["effort"] = eff
            rows.append(r)
            t = r.get("first") if a.full else r["total"]
            print(" " + pad(str(i), 5) + pad(secs(t), 9, True) + "   " + show(r))

    print()
    print(rule("요약"))
    print(" " + dim(pad("effort", 8) + pad("첫 소리 중앙값" if a.full else "첫 결정 중앙값", 16, True) + "   결과"))
    for eff in efforts:
        rs = [r for r in rows if r["effort"] == eff and not r.get("error")]
        totals = [r.get("first") if a.full else r["total"] for r in rs]
        totals = [t for t in totals if t is not None]
        n_exec = sum(1 for r in rs if executed(r))
        n_block = sum(1 for r in rs if attempted(r) and not executed(r))
        note = f"실제 전송 {n_exec}/{len(rs)}회"
        if gate:
            note += f", 관문 차단 {n_block}회"
        note += f", 말로만 응답 {sum(1 for r in rs if not attempted(r))}회"
        errs = sum(1 for r in rows if r["effort"] == eff and r.get("error"))
        if errs:
            note += red(f", 오류 {errs}회")
        if case and case.get("expect") and rs:
            ok = all(executed(r) == case["expect"]["execute"] for r in rs)
            note += "   " + (green("기대와 일치") if ok else red("기대와 다름"))
        print(" " + pad(eff, 8) + pad(secs(statistics.median(totals) if totals else None), 16, True) + "   " + note)


async def suite(a, base, kind, tools_raw):
    cases = read_cases(kind)
    eff = (a.effort.split(",")[0].strip() if a.effort else "") or base.llm_reasoning_effort
    cfg = dataclasses.replace(base, llm_reasoning_effort=eff)
    llm = OpenAICompatLLM(cfg)
    print(f" {bold('묶음')}    {kind} ({len(cases)}문항 × {a.repeat}회)  effort={eff}  관문="
          + ("꺼짐" if a.no_gate else "켜짐") + "  모드=" + ("끝까지" if a.full else "첫 결정"))
    summary = []
    for case in cases:
        say, start = build(case, a)
        gate = None if a.no_gate else Gate(policy.load_rules(cfg.rules_path), start)
        tools = gate.augment(tools_raw) if gate else tools_raw
        _, messages = messages_for(cfg, say, start)
        e = case.get("expect") or {}
        print()
        print(rule(f"{case['case_id']}  {case.get('label', '')}"))
        if start["history"]:
            for line in policy.format_history(start["history"]).splitlines()[-1:]:
                print("   " + dim("직전 " + line))
        print(f"   발화 \"{say}\"" + dim(f"   기대: {'실행' if e.get('execute') else '실행 안 함'} · {e.get('reply', '')}"))
        hits = 0
        for i in range(1, a.repeat + 1):
            r = await run_one(a, cfg, llm, messages, tools, gate, say, start)
            ok = (executed(r) == e.get("execute")) if e else None
            hits += bool(ok)
            mark = "" if ok is None else (green(" ✓") if ok else red(" ✗"))
            t = r.get("first") if a.full else r.get("total")
            print(" " + pad(str(i), 4) + pad(secs(t), 8, True) + mark + "  " + show(r))
        summary.append((case["case_id"], case.get("label", ""), hits))
    print()
    print(rule("요약"))
    for cid, label, hits in summary:
        color = green if hits == a.repeat else (yellow if hits else red)
        print("  " + pad(cid, 5) + pad(label, 34) + color(f"{hits}/{a.repeat} 기대와 일치"))
    total = sum(h for *_, h in summary)
    print("  " + bold(f"전체 {total}/{len(summary) * a.repeat}"))


async def main():
    ap = argparse.ArgumentParser(description="LLM 결정 + 실행 전 관문 확인 (상담원과 같은 프롬프트·파라미터)")
    ap.add_argument("--case", help="문항 ID: 팀 연습 K01~K08, 관문 시험 G01~G09")
    ap.add_argument("--suite", choices=list(CASE_FILES), help="묶음 전체 실행 (gate: G01~G09)")
    ap.add_argument("--say", help="고객 발화 문장 (--case보다 우선)")
    ap.add_argument("--history", help='이전 대화 JSON. 예: [{"role":"agent","text":"..."}]')
    ap.add_argument("--caller", help='발신자 정보 JSON. 예: {"phone":"01000001234"}')
    ap.add_argument("--now", help=f"현재 시각 (기본 {DEFAULT_NOW})")
    ap.add_argument("--repeat", type=int, help="반복 횟수 (기본: 문항 하나 3회, 묶음 1회)")
    ap.add_argument("--effort", default="", help="쉼표로 구분한 reasoning_effort 목록 (예: none,low). 비우면 .env 값")
    ap.add_argument("--no-gate", action="store_true", help="실행 전 관문 끄기 (개선 전 비교용)")
    ap.add_argument("--full", action="store_true",
                    help="상담원 대화 루프를 끝까지: 모의 데이터로 함수를 실제로 실행하고 결과 뒤의 답까지 출력")
    ap.add_argument("--show-prompt", action="store_true", help="조립된 프롬프트와 함수 정의를 함께 출력")
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args()
    a.repeat = a.repeat or (1 if a.suite else 3)

    if a.no_color or not sys.stdout.isatty():
        C.on = False
    if os.name == "nt":
        os.system("")  # Windows 콘솔에서 색상 코드 켜기

    load_env_file(a.env_file)
    base = Config.from_env()
    if not any(os.environ.get(k) for k in ("AGENT_PACK_DIR", "AGENT_RULES_PATH", "AGENT_PROMPT_PATH")):
        # the built-in K / G cases are practice data (DEMO numbers, explicit read-back): practice pack.
        # Official practice cases: team.scripts.llm_cases --kit <day kit>
        base = dataclasses.replace(base, **PRACTICE_PATHS)
    tools_raw = load_tools()
    efforts = [e.strip() for e in a.effort.split(",") if e.strip()] or [base.llm_reasoning_effort]
    key_ok = bool(os.environ.get(base.llm_api_key_env or ""))

    print()
    print(bold("══ LLM 확인 ") + dim("═" * 52))
    print(f" {bold('모델')}    {base.llm_model}  {dim('(' + base.llm_base_url + ')')}")
    print(f" {bold('키')}      {base.llm_api_key_env}: " + (green("있음") if key_ok else red("없음")))
    print(f" {bold('함수')}    " + ", ".join(t["function"]["name"] for t in tools_raw) + dim("  (data/demo_tools.json)"))
    print(f" {bold('규칙')}    {Path(base.rules_path).name}" + dim(f"  지침 {Path(base.prompt_path).name}"))
    if not key_ok and not a.show_prompt:
        print(red("\n 키가 없어 중단합니다. .env의 키 줄을 확인하세요."))
        return
    if a.suite:
        await suite(a, base, a.suite, tools_raw)
    else:
        await single(a, base, efforts, load_case(a.case) if a.case else None, tools_raw)
    print()
    if a.full:
        print(dim(" 첫 소리: 고객 발화를 넘긴 뒤 첫 문장이 나온 시각(STT·TTS 제외). 함수는 팀 모의 데이터로 실제 실행했다."))
    else:
        print(dim(" 첫 결정: 모델의 첫 응답(말 또는 함수 호출)을 다 받은 시각. 함수 호출은 다 받은 뒤 한 번에 넘어온다."))
        print(dim(" 관문을 통과한 함수는 이 모드에서 실제로 부르지 않는다(끝까지 보려면 --full)."))
    print(dim(" 시간은 OpenRouter 경유라 기능 확인용이다."))
    print()


if __name__ == "__main__":
    asyncio.run(main())
