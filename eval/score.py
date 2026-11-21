"""채점기 — run_eval.py 결과를 받아 응대·함수·속도 점수와 총점을 낸다.

OPENAI_API_KEY=... python3 score.py --run runs/try1 --cases ../data/practice_cases.jsonl
키가 없으면(또는 --no-judge) 응대 채점을 건너뛰고 함수·속도 점수만 낸다. 총점은 응대가 있어야 나온다.
결과: <run>/scored.jsonl (문항별 판정), <run>/summary.json (요약), 화면에 요약 출력

채점 방식은 ../SCORING.md 와 같다.
  1. 답 음성을 받아쓴다 (gpt-4o-transcribe)
  2. 채점 AI(기본 gpt-5.5)가 상담 시나리오·브랜드 정보·대화를 보고 응대를 맞음/괜찮음/틀림으로 판정한다
  3. 함수 호출은 정답(실제 상담에서의 호출)과 함수 이름·번호를 비교한다
"""
import argparse, asyncio, json, os, re, statistics
import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.path.join(HERE, "..", "data", "brand")
KEY = lambda: os.environ["OPENAI_API_KEY"]
HAS_KEY = lambda: bool(os.environ.get("OPENAI_API_KEY"))
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-5.5")
# 총점 = 응대 × 0.5 + 함수 × 0.2 + 속도 × 0.3  (모두 100점 만점)
W_RESP, W_TOOL, W_SPEED = 0.5, 0.2, 0.3
# 속도 점수표: (이 시간 이하, 점수). 표에 없는 느린 응답·소리 없음·끼어듦은 0점
SPEED_TABLE = [(1.5, 100), (2.0, 90), (2.5, 80), (3.0, 70), (3.5, 60), (4.0, 50), (5.0, 30), (6.0, 10)]        # 첫 소리 (손님 말 끝 기준)
AFTER_TOOL_TABLE = [(3.5, 100), (4.0, 90), (4.5, 80), (5.0, 70), (5.5, 60), (6.0, 50), (7.0, 30), (8.0, 10)]   # 조회 뒤 답 (마지막 함수 결과 기준)


def speed_points(sec, table=SPEED_TABLE):
    for limit, pts in table:
        if sec <= limit:
            return pts
    return 0
PTS = {"맞음": 1.0, "괜찮음": 0.7, "틀림": 0.0}
# 함수별로 "누구의 것인가"를 가르는 번호 인수 — 이 값이 정답과 같아야 맞은 호출이다
ID_ARGS = ["order_id", "buyer_cellphone", "customer_phone_number", "phone_number", "order_number"]
digits = lambda v: re.sub(r"\D", "", str(v or ""))

JUDGE_PROMPT = """너는 쇼핑몰 전화 상담 AI의 응대를 채점한다.

## 채점 기준
기준은 아래 [상담 시나리오]와 [브랜드 정보]다. 시나리오는 상황별 응대의 큰 흐름이며, 대화 내역으로 지금 어느 상황인지 판단한다.
[실제 상담 답]은 같은 상황에서 실제 상담에서 나간 응대다. 참고만 하라 — 이 답도 틀릴 수 있고, 다른 표현이나 다른 순서로도 맞을 수 있다.
전화 통화이므로 링크를 읽어주지 않거나, 문장이 짧거나, 정해진 인사말이 빠진 것은 감점하지 않는다.
"잠시만 기다려 주세요" 같은 채움말은 감점하지 않는다. 채움말 뒤의 실제 응대를 본다.

## 판정
- 맞음: 시나리오의 현재 상황에 맞게 응대했고 사실관계(가격·정책·절차·조회 결과)가 맞다.
- 괜찮음: 표현·순서가 조금 다르거나 하나를 덜 말했지만 고객이 받아도 문제없다.
- 틀림: 사실이 틀렸거나, 필요한 확인을 건너뛰거나 엉뚱한 상황으로 응대했거나, 함수를 불러야 할 때 안 불렀거나 받아야 할 정보 없이 불렀거나, 고객 질문에 답하지 않았거나, 사람 상담원에게 넘겨야 할 때 넘기지 않았다(또는 그 반대).

## 출력
JSON: {{"verdict": "맞음|괜찮음|틀림", "reason": "한두 문장"}}

[상담 시나리오]
{scenarios}

[브랜드 정보]
{info}

[현재 시각] {now}

[발신자 정보 (상담 AI도 받은 정보)] {caller}

[대화 내역]
{history}

[고객의 마지막 말 (원문)]
{utterance}

[실제 상담 답]
{reference}
{rubric}
[채점할 AI 응대]
{agent}
"""


def fmt_history(h):
    out = []
    for x in h:
        if x["role"] == "customer": out.append("고객: " + x["text"])
        elif x["role"] == "agent": out.append("상담원: " + x["text"])
        elif x["role"] == "tool_call": out.append(f"({x['name']} 함수 호출, 인수: {json.dumps(x['arguments'], ensure_ascii=False)})")
        elif x["role"] == "tool_result": out.append(f"({x['name']} 함수 결과: {json.dumps(x['result'], ensure_ascii=False)[:3000]})")
    return "\n".join(out) or "(없음)"


INFO_FUNCS = {"search_products"}   # 참고용 — 함수 점수에서 뺀다


LOOKUP_FUNCS = {"get_order_info", "get_return_info"}   # 손님 기록을 읽기만 하는 조회 함수


def tool_match(ref_calls, got_calls, history=()):
    """손님 기록을 조회·변경하는 함수를 본다.
    - 정답 호출은 모두 해야 하고, 번호 인수(주문번호·전화번호)가 정답과 같아야 한다.
    - 정답에 없는 접수 함수(취소·반품·교환·콜백)를 부르면 틀림.
    - 조회 함수를 더 부른 것은 감점하지 않는다. 이미 이전 대화에 같은 조회 결과가 있으면 다시 조회하지 않아도 된다.
    - 상품 검색 같은 참고용 함수는 불러도 안 불러도 함수 점수에 영향이 없다 (응대 판정에서 본다)."""
    ref_calls = [c for c in ref_calls if c["name"] not in INFO_FUNCS]
    got_calls = [c for c in got_calls if c["name"] not in INFO_FUNCS]
    seen = {h.get("name") for h in history if h.get("role") == "tool_result"}
    ref_names = {c["name"] for c in ref_calls}
    extra = sorted({c["name"] for c in got_calls} - ref_names - LOOKUP_FUNCS)
    if extra:
        return False, f"부르지 말아야 할 접수 함수: {extra}"
    for rc in ref_calls:
        optional = rc["name"] in LOOKUP_FUNCS and rc["name"] in seen
        called = [c for c in got_calls if c["name"] == rc["name"]]
        if not called:
            if optional:
                continue
            return False, f"{rc['name']} 호출 안 함"
        want = {k: digits(rc["arguments"].get(k)) for k in ID_ARGS if digits(rc["arguments"].get(k))}
        if want and not any(any(digits(c["arguments"].get(k)) == v for k, v in want.items()) for c in called):
            return False, f"{rc['name']} 번호 다름"
    return True, ""


async def transcribe(c, path):
    if not os.path.exists(path) or os.path.getsize(path) < 3200:
        return ""
    r = await c.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": "Bearer " + KEY()},
                     data={"model": "gpt-4o-transcribe", "language": "ko"},
                     files={"file": (os.path.basename(path), open(path, "rb"), "audio/wav")})
    r.raise_for_status()
    return r.json()["text"]


async def judge(c, case, said, calls, ctx):
    ref = case["reference"]
    ref_txt = "".join(f"({t['name']} 함수 호출, 인수: {json.dumps(t['arguments'], ensure_ascii=False)})\n" for t in ref["tool_calls"])
    ref_txt += "".join(f"({t['name']} 함수 결과: {json.dumps(t.get('result'), ensure_ascii=False)[:1500]})\n" for t in ref.get("tool_results", []))
    ref_txt += ref.get("answer") or ""
    rb = ctx["rubric"].get(case["case_id"])
    rubric = "" if not rb else ("\n[이 문항 채점 기준 — 실제 상담 답보다 이 기준을 우선한다]\n"
        f"상황: {rb['situation']}\n정답 함수: {rb['tool']}\n꼭 할 것:\n" + "".join(f"- {x}\n" for x in rb["must"])
        + "하면 안 되는 것:\n" + "".join(f"- {x}\n" for x in rb["must_not"]) + (f"참고: {rb['note']}\n" if rb.get("note") else "")
        + "판정: 꼭 할 것을 다 했고 하면 안 되는 것이 없으면 맞음, 덜 중요한 하나를 빠뜨렸으면 괜찮음, 핵심을 놓쳤거나 하면 안 되는 것을 했으면 틀림.\n")
    ag = "".join(f"({t['name']} 함수 호출, 인수: {json.dumps(t['arguments'], ensure_ascii=False)}, 조회 성공: {t['matched']})\n" for t in calls)
    ag += "말한 내용: " + (said or "(아무 말도 하지 않음)")
    prompt = JUDGE_PROMPT.format(scenarios=ctx["scenarios"], info=ctx["info"], now=case["now"], caller=json.dumps(case.get("customer_profile"), ensure_ascii=False), history=fmt_history(case["history"]),
                                 utterance=case["customer_utterance"], reference=ref_txt, rubric=rubric, agent=ag)
    body = {"model": JUDGE_MODEL, "messages": [{"role": "user", "content": prompt}], "reasoning_effort": "low",
            "response_format": {"type": "json_schema", "json_schema": {"name": "v", "strict": True, "schema": {
                "type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
                "properties": {"verdict": {"type": "string", "enum": list(PTS)}, "reason": {"type": "string"}}}}}}
    for _ in range(3):
        r = await c.post("https://api.openai.com/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + KEY()})
        if r.status_code == 200:
            return json.loads(r.json()["choices"][0]["message"]["content"])
        await asyncio.sleep(3)
    return {"verdict": "틀림", "reason": f"채점 실패 {r.status_code}"}


def pct(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p * len(a)))] if a else None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--rubric", default="", help="문항별 채점 기준 JSON (선택). 있으면 채점 AI가 이 기준을 우선한다")
    ap.add_argument("--no-judge", action="store_true", help="응대 채점(OpenAI) 없이 함수·속도 점수만 낸다")
    a = ap.parse_args()
    judge_on = HAS_KEY() and not a.no_judge
    if not judge_on:
        print("※ OPENAI_API_KEY 가 없거나 --no-judge — 응대 채점은 건너뛰고 함수·속도 점수만 냅니다.\n")
    cases = {c["case_id"]: c for c in map(json.loads, open(a.cases, encoding="utf-8"))}
    results = [json.loads(l) for l in open(os.path.join(a.run, "results.jsonl"), encoding="utf-8")]
    scen = open(os.path.join(BRAND, "scenarios.md"), encoding="utf-8").read()
    info = json.load(open(os.path.join(BRAND, "information.json"), encoding="utf-8"))
    ctx = {"scenarios": scen,
           "info": "\n".join(f"{i['tag']}\n{i['content']}" for i in info),
           "rubric": json.load(open(a.rubric, encoding="utf-8")) if a.rubric else {}}
    sem = asyncio.Semaphore(a.concurrency)

    async def one(c, r):
        async with sem:
            case = cases[r["case_id"]]
            if "error" in r:
                return {**r, "verdict": "틀림", "reason": "연결 실패", "tool_ok": False}
            ok, why = tool_match(case["reference"]["tool_calls"], r.get("tool_calls", []), case.get("history", []))
            for alt in (ctx["rubric"].get(case["case_id"]) or {}).get("alt_tool_calls", []):   # 기준표에 적힌 다른 정답 호출
                if not ok and tool_match(alt, r.get("tool_calls", []), case.get("history", []))[0]:
                    ok, why = True, "다른 정답 호출과 일치"
            if not judge_on:
                return {**r, "tool_ok": ok, "tool_why": why, "verdict": None, "reason": "응대 채점 안 함"}
            said = await transcribe(c, os.path.join(a.run, "agent_audio", r["case_id"] + ".wav"))
            v = await judge(c, case, said, r.get("tool_calls", []), ctx)
            return {**r, "said": said, "tool_ok": ok, "tool_why": why, **v}

    async with httpx.AsyncClient(timeout=180) as c:
        scored = await asyncio.gather(*[one(c, r) for r in results])
    inf = float("inf")
    for s in scored:
        first = s.get("first_sound") if s.get("first_sound") is not None and not s.get("spoke_before_end") else inf
        s["speed_points"] = speed_points(first)
        if s.get("tool_calls"):
            after = s["after_tool_sound"] if s.get("after_tool_sound") is not None else inf
            s["speed_points"] = (speed_points(first) + speed_points(after, AFTER_TOOL_TABLE)) / 2
    with open(os.path.join(a.run, "scored.jsonl"), "w", encoding="utf-8") as f:
        for s in scored:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    fs = [s.get("first_sound") if s.get("first_sound") is not None and not s.get("spoke_before_end") else inf for s in scored]
    at = [s["after_tool_sound"] if s.get("after_tool_sound") is not None else inf for s in scored if s.get("tool_calls")]
    resp = statistics.mean(PTS[s["verdict"]] for s in scored) * 100 if judge_on else None
    tool = statistics.mean(1.0 if s["tool_ok"] else 0.0 for s in scored) * 100
    speed = statistics.mean(s["speed_points"] for s in scored)
    summ = {"cases": len(scored),
            "first_sound_p50": pct(fs, .5), "first_sound_p90": pct(fs, .9),
            "after_tool_p50": pct(at, .5), "after_tool_p90": pct(at, .9),
            "response_score": round(resp, 1) if judge_on else None, "tool_score": round(tool, 1), "speed_score": round(speed, 1),
            "total": round(resp * W_RESP + tool * W_TOOL + speed * W_SPEED, 1) if judge_on else None,
            "verdicts": {k: sum(s["verdict"] == k for s in scored) for k in PTS} if judge_on else None}
    json.dump(summ, open(os.path.join(a.run, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    f = lambda x: "없음" if x is None else ("—" if x == inf else f"{x:.2f}초")
    print(f"문항 {summ['cases']}개")
    print(f"첫 소리       보통 {f(summ['first_sound_p50'])} / 느린 10% {f(summ['first_sound_p90'])}")
    print(f"조회 뒤 답    보통 {f(summ['after_tool_p50'])} / 느린 10% {f(summ['after_tool_p90'])}")
    if judge_on:
        print(f"응대 {summ['response_score']} × 0.5 + 함수 {summ['tool_score']} × 0.2 + 속도 {summ['speed_score']} × 0.3"
              f" = 총점 {summ['total']}   {summ['verdicts']}")
    else:
        print(f"함수 {summ['tool_score']} / 속도 {summ['speed_score']}   (응대 채점 안 함 — 총점 없음)")


if __name__ == "__main__":
    asyncio.run(main())
