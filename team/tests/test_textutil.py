from team.agent.textutil import SentenceSplitter, looks_complete, normalize_for_tts


def test_complete_sentences():
    for t in ["배송 언제 와요?", "주문 취소하고 싶어요.", "환불 가능한가요", "상담원 연결해 주세요.",
              "효과적인 복용 방법은 무엇입니까?", "택배 상자가 없어서 그냥 내놓으면 되겠죠?"]:
        assert looks_complete(t) is True, t


def test_incomplete_sentences():
    for t in ["어제 주문했는데", "주문번호는", "그리고", "제가 음", "배송이 늦어서", "주문한 거랑...", "그러니까,"]:
        assert looks_complete(t) is False, t


def test_unknown_waits():
    assert looks_complete("") is None
    assert looks_complete("01012345678") is None  # number being read out


def test_sentence_splitter_streams():
    s = SentenceSplitter()
    out = []
    for d in ["주문번호 DEMO1001 건은 ", "배송중입니다. 다른", " 문의 있으세요? "]:
        out += s.push(d)
    out += s.flush()
    assert out == ["주문번호 DEMO1001 건은 배송중입니다.", "다른 문의 있으세요?"]


def test_sentence_splitter_merges_short_and_waits_on_trailing_dot():
    s = SentenceSplitter()
    assert s.push("네. ") == []
    assert s.push("확인했습니다.") == []          # trailing punctuation: wait
    assert s.push(" 감사합니다.") == ["네. 확인했습니다."]
    assert s.flush() == ["감사합니다."]


def test_tts_normalization_reads_numbers_digit_by_digit():
    assert normalize_for_tts("01000001234로 연락") == "공 일 공 공 공 공 공 일 이 삼 사로 연락"
    assert normalize_for_tts("DEMO1001 건") == "DEMO 일 공 공 일 건"
    assert normalize_for_tts("**3개**") == "3개"
