"""One rules file (pack): gate rules, hand-off signals and the prompt's function-rules section
all come from the same place, and a pack folder switches every brand-specific file at once."""
import json

from team.agent import policy
from team.agent.config import Config
from team.agent.gate import CONFIRM_FIELD, Gate

PRACTICE = policy.load_rules(Config().rules_path)


def test_practice_pack_is_the_default_and_complete():
    for key in ("functions", "_default", "handoff", "consent", "arg_labels"):
        assert key in PRACTICE


def test_prompt_section_is_generated_from_the_same_table_the_gate_uses():
    text = policy.function_rules_text(PRACTICE)
    for name in PRACTICE["functions"]:
        assert name in text
    assert "get_order_info (조회): 주문번호·주문자 전화번호가 대화에 나오면 바로 호출한다." in text  # either number (official get_order_info)
    # request_callback has a confirm_template: the code reads the number back, not the model
    assert "request_callback (접수·변경): 고객이 원하면 전화번호 확인 질문을 직접 말하지 말고" in text
    assert "시스템이 정확한 값으로 확인 질문을 대신 한다" in text
    no_tpl = json.loads(json.dumps(PRACTICE))
    no_tpl["functions"]["request_callback"].pop("confirm_template")
    assert "request_callback (접수·변경): 직전에 전화번호를 복창해" in policy.function_rules_text(no_tpl)
    prompt = policy.build_system_prompt("지침 {now}", {"now": "2026-11-21 11:00"}, {}, PRACTICE)
    assert prompt.index("지침") < prompt.index("[함수 실행 규칙")  # static part stays in front


def test_changing_confirm_in_rules_changes_both_prompt_and_gate():
    rules = json.loads(json.dumps(PRACTICE))
    rules["functions"]["request_callback"]["confirm"] = "request"
    assert "직접 말하며 요청했을 때만" in policy.function_rules_text(rules)
    g = Gate(rules, {"history": [], "caller": None})
    args = json.dumps({"phone_number": "01012345678", CONFIRM_FIELD: "부탁드려요"})
    assert g.check("request_callback", args, "공일공 일이삼사 오육칠팔로 콜백 부탁드려요.").allowed
    assert not Gate(PRACTICE, {"history": [], "caller": None}).check(
        "request_callback", args, "공일공 일이삼사 오육칠팔로 콜백 부탁드려요.").allowed


def test_handoff_signals_come_from_the_rules_file():
    assert policy.handoff_flags("상담원이랑 통화하고 싶어요", PRACTICE) == ["human_request"]
    rules = {"handoff": {"refund_dispute": {"label": "환불 분쟁", "pattern": "환불.*안\\s*해"}}}
    assert policy.handoff_flags("환불을 안 해 줘요", rules) == ["refund_dispute"]
    assert "환불 분쟁" in policy.user_message("환불을 안 해 줘요", ["refund_dispute"], rules)
    assert policy.handoff_flags("상담원 연결", None) == ["human_request"]  # built-in fallback


def test_consent_words_come_from_the_rules_file():
    rules = json.loads(json.dumps(PRACTICE))
    rules["consent"]["negative"] = "글쎄"
    hist = [{"role": "agent", "text": "지금 거신 번호 01000001234로 연락드려도 될까요?"}]
    g = Gate(rules, {"history": hist, "caller": {"phone": "01000001234"}})
    args = json.dumps({"phone_number": "01000001234", CONFIRM_FIELD: "네"})
    assert g.check("request_callback", args, "네 글쎄요").reason == "hesitation"


def test_pack_dir_switches_all_files_but_explicit_paths_win(tmp_path, monkeypatch):
    (tmp_path / "pack.json").write_text(json.dumps(
        {"name": "official", "prompt": "p.md", "rules": "r.json", "phrases": "ph.json"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_PACK_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.prompt_path == str(tmp_path / "p.md")
    assert cfg.rules_path == str(tmp_path / "r.json")
    assert cfg.phrases_path == str(tmp_path / "ph.json")
    monkeypatch.setenv("AGENT_RULES_PATH", "C:/elsewhere/rules.json")
    cfg = Config.from_env()
    assert cfg.rules_path == "C:/elsewhere/rules.json" and cfg.prompt_path == str(tmp_path / "p.md")


def test_practice_pack_manifest_points_at_existing_files():
    cfg = Config()
    cfg.apply_pack(str(Config().rules_path).rsplit("practice_rules.json", 1)[0])
    for path in (cfg.prompt_path, cfg.rules_path, cfg.phrases_path):
        assert open(path, encoding="utf-8").read()
