# 말 끝 판단(발화 종료) 실험 · Soniox/Gemini 음성 서버

11/19 운영진 공지로 바뀐 최종 모델 기준이다: STT Soniox, TTS Gemini 2.5 Flash TTS, LLM gpt-5.4 mini.

스타터킷 원본과 `team/agent`는 건드리지 않았다. 추가한 파일은 아래 표와 같다.

## 결론 먼저

침묵 길이만으로는 끼어들기와 속도를 같이 잡을 수 없다. 1.3초가 침묵 방식의 최선(18개 중 2개 끼어듦)이지만, 문장이 끝난 뒤 쉬었다가 이어 말하는 경우(KO7)는 못 막는다. 그래서 의미로 말 끝을 판단하는 Soniox 말 끝 감지를 쓰고, 토요일에 같은 18개 음성으로 비교한다.

| 기다리는 시간 | 끼어든 문항 | 평균 첫 음성 | 평균 속도 점수 |
|---|---|---|---|
| 0.5초 | 6개 (KO5, KO7, KO9 + 잡음 버전) | 0.48초 | 66.7 |
| 0.8초 | 6개 | 0.78초 | 66.7 |
| 1.0초 | 6개 | 0.98초 | 66.7 |
| 1.3초 | 2개 (KO7, KO7N) | 1.28초 | 88.9 |
| 1.5초 | 2개 (KO7, KO7N) | 1.48초 | 88.9 |

- 고객이 쉬는 시간이 기다리는 시간보다 길면 무조건 끼어든다.
- 첫 음성 시간은 AI 없이 삐 소리만 낸 값이다. 실제로는 STT·LLM·TTS 시간이 더해진다.
- 잡음 버전은 침묵 방식에서는 차이가 없었다. 토요일 Soniox 인식 정확도 확인용이다.
- 원자료: `team/runs/endpoint_sweep_dry_1119/summary.md`, `cases.csv`

## 파일

| 파일 | 하는 일 |
|---|---|
| `team/scripts/endpoint_server.py` | 비교 실험용 음성 서버. Soniox로 계속 보내며 `<end>`(말 끝)에서 답을 만들고, Gemini TTS 음성(24kHz)을 16kHz로 바꿔 보낸다. `--dry`는 키 없이 침묵 길이로 판단 |
| `team/scripts/make_endpoint_audio.py` | 한국어 테스트 음성 18개 만들기 (맥 `say` 필요) |
| `team/scripts/sweep_endpoint.py` | 말 끝 설정을 바꿔가며 자동 측정, 설정별 표 만들기 |
| `team/data/endpoint_ko/` | 테스트 음성 18개(`audio/`)와 측정기용 문항 목록(`cases.jsonl`) |
| `team/runs/endpoint_sweep_dry_1119/` | 11/19 침묵 길이 비교 결과 |

## 테스트 음성 (기계 음성, 맥 `say` Yuna)

| 음성 | 내용 | 시험하는 것 |
|---|---|---|
| KO1 | 주문번호 공일공 삼사오인데요, 언제 도착하나요? | 기본 배송 조회 |
| KO2 | 어제 주문 취소했는데 아직 환불이 안 들어왔어요. 언제 들어와요? | 긴 문장, 두 문장 |
| KO3 | 아까 그거 취소된 거 맞아요? | 짧은 문장, 이전 대화 필요 |
| KO4 | 어제부터 이 제품 먹었는데 속이 좀 안 좋고 두드러기가 났어요. | 인계 상황 |
| KO5 | 주문번호가 (1.2초) 공일공 삼사오예요. 배송 어디쯤 왔는지 알려주세요. | 번호 앞에서 쉼 |
| KO6 | 주문번호 공일공 삼사오, 아 아니다, 공일공 삼사육이요. 배송 조회해주세요. | 번호 정정 |
| KO7 | 배송 언제 와요? (1.5초) 아 그리고 주문번호는 공일공 삼사오예요. | 문장이 끝난 뒤 쉬고 이어 말함 |
| KO8 | 네, 취소요. | 아주 짧게 끝남 |
| KO9 | 주문번호 공일공 (1초) 삼사오 (1초) 육칠팔이요. | 번호를 끊어서 부름 |

KO1N~KO9N은 전화 음질(8kHz 대역, 300~3400Hz) + 잡음 버전이다. 기계 음성이라 인식이 실제보다 잘 될 수 있으니, 인식 정확도는 사람 음성과 토요일 연습 문제로 다시 본다.

## 실행 (저장소 루트에서)

```bash
pip install websockets httpx numpy scipy

# 키 없이 시험 (말 끝 = 침묵 길이, 답 = 삐 소리)
python team/scripts/endpoint_server.py --port 9000 --dry

# 키가 있을 때
export SONIOX_API_KEY=...
export GEMINI_API_KEY=...
python team/scripts/endpoint_server.py --port 9000

# 측정 (함수 서버 8100이 켜져 있어야 함)
python eval/run_eval.py --server ws://127.0.0.1:9000 --cases team/data/endpoint_ko/cases.jsonl \
  --audio team/data/endpoint_ko/audio --mock http://127.0.0.1:8100 --out team/runs/endpoint_test

# 설정별 자동 비교 (함수 서버·음성 서버를 알아서 띄움)
python team/scripts/sweep_endpoint.py                                  # 키 없이, 침묵 길이 비교
python team/scripts/sweep_endpoint.py --soniox --values 500 1000 1500  # 키 있을 때, Soniox 비교
```

## endpoint_server.py 흐름

1. 측정기 음성(16kHz)을 Soniox로 그대로 보낸다 (`pcm_s16le`, 16000Hz, 변환 없음).
2. Soniox가 `<end>` 토큰을 보내면 바로 답 만들기를 시작한다. LLM 자리는 `make_reply()`이고, 지금은 들은 말을 따라 말한다.
3. 답의 첫 문장과 나머지를 동시에 Gemini TTS로 만들고, 24kHz → 16kHz로 바꾼다.
4. 고객이 다시 말하면(새 토큰) 만들던 답을 버린다.
5. 안전장치: 우리가 직접 잰 침묵이 0.3초보다 짧으면 말하지 않는다.
6. 음성을 다 보내면 `turn.end`.

## 조절할 값 (환경 변수)

| 값 | 기본 | 의미 |
|---|---|---|
| `MAX_ENDPOINT_DELAY_MS` | 1500 | Soniox 말 끝 최대 대기 (500~3000) |
| `ENDPOINT_LATENCY` | 2 | Soniox `endpoint_latency_adjustment_level` (0~3, 높을수록 빨리) |
| `ENDPOINT_SENSITIVITY` | 0.3 | Soniox `endpoint_sensitivity` (-1.0~1.0) |
| `MIN_SILENT_MS` | 300 | 말하기 전 최소 침묵 |
| `DRY_END_MS` | 1300 | `--dry`에서 말 끝으로 볼 침묵 |
| `GEMINI_TTS_MODEL`, `GEMINI_VOICE` | gemini-2.5-flash-preview-tts, Kore | TTS 모델과 목소리 |

Soniox 추천 시작값(latency 2, sensitivity 0.3, max_delay 1500)을 기본으로 했다. 출처: [Soniox 말 끝 감지](https://docs.soniox.com/stt/rt/endpoint-detection), [Soniox WebSocket API](https://docs.soniox.com/api-reference/stt/websocket-api).

## 아직 못 한 것 (키 필요, 토요일)

- Soniox·Gemini 실제 연결 확인. 문서대로 짰지만 실제로 돌려보지 못했다.
- Gemini 2.5 Flash TTS가 스트리밍을 지원하는지. 지금은 첫 문장을 따로 먼저 합성하는 방식이다.
- KO6 번호 정정을 STT가 제대로 알아듣는지, TTS 첫 소리까지 걸리는 시간.
- 크레딧이 회사별 키로 오는지, 하나의 플랫폼으로 오는지에 따라 연결 부분을 바꿔야 할 수 있다.
