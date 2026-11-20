// Voice test page: mic -> 16 kHz s16le mono 640-byte frames -> WebSocket; server PCM -> ordered playback.
// Only states the server actually reports are shown as facts; browser-side estimates are labelled.
import { StreamResampler, S16Decoder, FRAME_BYTES, FRAME_SAMPLES, TARGET_RATE, rmsInt16LE } from './pcm.js';
import {
  SPEED_TABLE, SILENCE_RMS, RESPONSE_OPTIONS, FUNC_OPTIONS, caseSpeed, systemScore, averageScores, parseJudgment,
} from './score.js';

const $ = (id) => document.getElementById(id);
const TAIL_TIMEOUT_MS = 25000;    // starter-kit evaluator gives up 25 s after the end of speech
const MAX_TEST_MS = 120000;       // hard cap for one test
const CONNECT_TIMEOUT_MS = 5000;
const AFTER_DONE_WAIT_MS = 3000;  // debug mode: wait for metrics / function-server record before closing
const REPLAY_MAX_FRAMES = 30 * 50;
const LOG_MAX = 600;

let T = null;            // current test
let lastResult = null;   // { userText, agentText } of the last finished turn
let replayFrames = null; // frames sent in the last test (memory only)
let replayCtx = null;

// ── settings (only non-sensitive values are remembered; history/caller are not stored) ──
const PERSIST = ['mode', 'wsUrl', 'toolsUrl', 'vadThr', 'ec', 'ns', 'agc', 'useHistory', 'micDevice'];
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('devui.settings') || '{}');
    for (const k of PERSIST) if (k in s) { const el = $(k); if (el.type === 'checkbox') el.checked = s[k]; else el.value = s[k]; }
  } catch { /* storage unavailable: defaults */ }
  if (!$('wsUrl').value) $('wsUrl').value = defaultWsUrl($('mode').value);
}
function saveSettings() {
  try {
    const s = {};
    for (const k of PERSIST) { const el = $(k); s[k] = el.type === 'checkbox' ? el.checked : el.value; }
    localStorage.setItem('devui.settings', JSON.stringify(s));
  } catch { /* ignore */ }
}
function defaultWsUrl(mode) {
  if (mode === 'compat') return 'ws://127.0.0.1:9000';
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host || '127.0.0.1:9100'}/ws`;
}
function nowString() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ── small DOM helpers (textContent only: server strings are never parsed as HTML) ──
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function pretty(v) {
  if (typeof v === 'string') { try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; } }
  return JSON.stringify(v, null, 2);
}
function canon(v) {
  if (typeof v === 'string') { try { v = JSON.parse(v); } catch { return v; } }
  const sort = (x) => Array.isArray(x) ? x.map(sort)
    : (x && typeof x === 'object') ? Object.fromEntries(Object.keys(x).sort().map((k) => [k, sort(x[k])])) : x;
  return JSON.stringify(sort(v));
}

// ── status ──
const STATUS = {
  idle: ['연결 전', 's-idle'],
  mic: ['마이크 준비 중', 's-busy'],
  connecting: ['서버 연결 중', 's-busy'],
  listening: ['마이크 전송 중 · 말씀하세요', 's-live'],
  speech: ['고객 음성 수신 중 (서버 VAD 감지)', 's-live'],
  thinking: ['발화 종료 확정 · 응답 준비 중', 's-busy'],
  receiving: ['상담원 음성 수신 중', 's-live'],
  server_done: ['서버 응답 완료 (turn.end)', 's-ok'],
  done: ['테스트 완료', 's-ok'],
  stopped: ['중지됨', 's-idle'],
  error: ['오류', 's-err'],
};
const PLAYBACK = {
  idle: ['대기', 's-idle'], playing: ['재생 중', 's-live'], done: ['재생 완료', 's-ok'],
  none: ['받은 음성 없음', 's-busy'], stopped: ['중단', 's-idle'], error: ['재생 실패', 's-err'],
};
function setStatus(t, key, detail = '') {
  if (t !== T) return;
  t.status = key;
  const [label, cls] = STATUS[key];
  const s = $('status');
  s.textContent = label; s.className = 'pill ' + cls;
  $('statusDetail').textContent = detail;
}
function setPlayback(t, key, extra = '') {
  if (t !== T) return;
  t.playState = key;
  const [label, cls] = PLAYBACK[key];
  const p = $('playback');
  p.textContent = label + (extra ? ` · ${extra}` : ''); p.className = 'pill ' + cls;
}

function logEvt(t, dir, obj) {
  if (t !== T) return;
  const ms = Math.round(performance.now() - t.t0);
  const line = `${String(ms).padStart(6)} ms ${dir} ${typeof obj === 'string' ? obj : JSON.stringify(obj)}`;
  t.log.push(line);
  if (t.log.length > LOG_MAX) t.log.splice(0, t.log.length - LOG_MAX);
  $('log').textContent = t.log.join('\n');
}

// ── playback queue: chunks are decoded, resampled with continuous filter state, and scheduled back to back ──
class PcmPlayer {
  constructor(ctx, cb) {
    this.ctx = ctx; this.cb = cb;
    this.dec = new S16Decoder();
    this.rs = new StreamResampler(TARGET_RATE, ctx.sampleRate);
    this.next = 0; this.pending = 0; this.sources = new Set();
    this.started = false; this.ended = false; this.done = false; this.gaps = 0; this.seconds = 0;
  }
  push(ab) { this._schedule(this.rs.process(this.dec.push(ab))); }
  _schedule(f) {
    if (!f.length || this.done) return;
    const ctx = this.ctx, now = ctx.currentTime;
    const buf = ctx.createBuffer(1, f.length, ctx.sampleRate);
    buf.copyToChannel(f, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    if (this.next < now + 0.01) {               // queue ran dry (or first chunk): leave a little lead time
      if (this.started) this.gaps++;
      this.next = now + (this.started ? 0.02 : 0.06);
    }
    src.start(this.next);
    if (!this.started) {
      this.started = true;
      const outLat = ctx.outputLatency || ctx.baseLatency || 0;
      this.cb.onStart(performance.now() + (this.next - now + outLat) * 1000);
    }
    this.next += buf.duration; this.seconds += buf.duration; this.pending++;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); this.pending--; this._check(); };
  }
  end() { this.ended = true; this._schedule(this.rs.flush()); this._check(); }
  _check() { if (this.ended && this.pending === 0 && !this.done) { this.done = true; this.cb.onDone(this.started); } }
  queuedSec() { return this.started ? Math.max(0, this.next - this.ctx.currentTime) : 0; }
  stop() {
    this.done = true;
    for (const s of this.sources) { try { s.onended = null; s.stop(); } catch { /* already stopped */ } }
    this.sources.clear(); this.pending = 0;
  }
}

const GATE_REASONS = {
  invalid_arguments: '인자를 읽을 수 없음', invalid_format: '형식 불일치', not_grounded: '대화에 없는 값',
  hesitation: '거절·보류·정정 표현', puzzled: '고객이 되물음', no_consent: '이번 말에서 동의를 찾지 못함',
  not_asked: '직전 상담원 질문에 이 값이 없음', not_stated: '고객이 값을 직접 말하지 않음',
};
const BLOCKED_HOW = {
  already_asked: '모델이 이미 확인 질문을 말해서 턴 종료', template: '규칙 파일의 확인 문장을 바로 말함',
  model: '모델이 차단 안내를 보고 다음 라운드에서 답함',
};

// ── test lifecycle ──
function newTest() {
  return {
    t0: performance.now(), log: [], timers: [], status: 'idle', playState: 'idle',
    sid: `UI-${nowString().replace(/[-: ]/g, '')}-${Math.random().toString(16).slice(2, 6)}`,
    debug: false, hello: null, sending: false, everSent: false, micClosed: false, finalized: false,
    framesMade: 0, framesSent: 0, framesBeforeConnect: 0, badFrames: 0, bytesSent: 0, clipIn: 0, clipOut: 0,
    level: 0, maxBuffered: 0, replay: [],
    firstFrameSentAt: null, firstVoice: null, lastVoice: null,
    agentTexts: [], userText: null, dbg: [], tools: [], mockCalls: null, metrics: null, eou: null,
    gate: [], blocked: [], guardFlags: [], toolAck: null, eouMode: null,
    // practice score (browser clock): first voiced agent frame, last function result, mic voice times
    firstVoicedRecvAt: null, lastToolEndAt: null, afterToolVoicedAt: null, toolSent: false, micVoiceAt: [],
    firstAudioRecvAt: null, firstPlayAt: null, turnEndAt: null, playbackDoneAt: null,
    audioBytesRecv: 0, audioChunks: 0, audioAfterTurnEnd: 0,
  };
}

function micErrorText(e) {
  switch (e && e.name) {
    case 'NotAllowedError': case 'SecurityError':
      return '마이크 권한이 거부되었습니다. 주소창의 사이트 설정에서 마이크를 허용한 뒤 다시 시작하세요.';
    case 'NotFoundError': case 'OverconstrainedError':
      return '사용할 수 있는 마이크 장치가 없습니다. 선택한 장치가 빠졌다면 설정에서 "시스템 기본 장치"나 다른 마이크를 고르세요.';
    case 'NotReadableError': case 'AbortError': return '마이크를 열 수 없습니다 (다른 프로그램이 사용 중일 수 있음).';
    default: return `마이크 오류: ${e && e.name ? e.name + ': ' : ''}${e && e.message ? e.message : e}`;
  }
}

async function startTest() {
  if (T && !T.finalized) teardown(T, 'restart');
  const t = T = newTest();
  resetView(t);
  let caller, history;
  try { caller = JSON.parse($('caller').value.trim() || 'null'); }
  catch (e) { return fail(t, `caller JSON 형식 오류: ${e.message}`); }
  try {
    history = $('useHistory').checked ? JSON.parse($('history').value.trim() || '[]') : [];
    if (!Array.isArray(history)) throw new Error('배열([])이어야 합니다');
  } catch (e) { return fail(t, `history JSON 형식 오류: ${e.message}`); }
  const url = $('wsUrl').value.trim();
  if (!/^wss?:\/\//.test(url)) return fail(t, 'WebSocket URL은 ws:// 또는 wss://로 시작해야 합니다.');
  saveSettings();
  $('btnStart').disabled = true; $('btnStop').disabled = false; $('btnReplay').disabled = true;
  $('btnAddHistory').disabled = true;
  $('sid').textContent = t.sid;
  setStatus(t, 'mic', '마이크 권한을 확인하는 중');

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return fail(t, '이 주소에서는 마이크를 쓸 수 없습니다. http://127.0.0.1:9100 또는 http://localhost:9100(또는 HTTPS)으로 여세요.');
  }
  try {
    t.stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
  } catch (e) { return fail(t, micErrorText(e)); }
  refreshDevices();                             // labels become visible once permission is granted
  if (t !== T || t.finalized) { t.stream.getTracks().forEach((x) => x.stop()); return; }

  try {
    t.ctx = new AudioContext({ latencyHint: 'interactive' });
    await t.ctx.resume();
    await t.ctx.audioWorklet.addModule('capture-worklet.js');
    if (t !== T || t.finalized) return;
    t.src = t.ctx.createMediaStreamSource(t.stream);
    t.node = new AudioWorkletNode(t.ctx, 'pcm-capture');
    t.mute = t.ctx.createGain();
    t.mute.gain.value = 0;                      // keeps the graph pulled; nothing is played back
    t.src.connect(t.node); t.node.connect(t.mute); t.mute.connect(t.ctx.destination);
    t.node.onprocessorerror = () => fail(t, '오디오 변환 처리기(AudioWorklet)에서 오류가 났습니다.');
    t.node.port.onmessage = (e) => onWorklet(t, e.data);
  } catch (e) { return fail(t, `오디오 변환 초기화 실패: ${e.name || ''} ${e.message || e}`); }

  t.track = t.stream.getAudioTracks()[0];
  t.trackSettings = (t.track && t.track.getSettings) ? t.track.getSettings() : {};
  t.track.onended = () => { if (!t.micClosed && t === T) fail(t, '마이크 장치 연결이 끊겼습니다.'); };
  t.player = new PcmPlayer(t.ctx, {
    onStart: (at) => { t.firstPlayAt = at; setPlayback(t, 'playing'); renderTiming(t); },
    onDone: (any) => {
      t.playbackDoneAt = performance.now();
      setPlayback(t, any ? 'done' : 'none');
      renderTiming(t); maybeFinish(t);
    },
  });
  setStatus(t, 'connecting', '마이크 준비됨 · 서버에 연결하는 중');
  connect(t, url, {
    type: 'session.start', session_id: t.sid, case_id: 'DEVUI', now: $('now').value.trim() || nowString(),
    history, caller, tools_url: $('toolsUrl').value.trim() || null,
    audio: { encoding: 'pcm_s16le', sample_rate: TARGET_RATE, channels: 1 },
  });
  t.timers.push(setInterval(() => tick(t), 200));
}

function connect(t, url, startMsg) {
  let ws;
  try { ws = new WebSocket(url); } catch (e) { return fail(t, `WebSocket URL 오류: ${e.message}`); }
  ws.binaryType = 'arraybuffer';
  t.ws = ws;
  t.timers.push(setTimeout(() => {
    if (ws.readyState === WebSocket.CONNECTING) fail(t, `서버 연결 시간 초과 (${CONNECT_TIMEOUT_MS / 1000}초). 서버 실행 여부와 URL을 확인하세요.`);
  }, CONNECT_TIMEOUT_MS));
  ws.onopen = () => {
    if (t.finalized) return;
    ws.send(JSON.stringify(startMsg));
    logEvt(t, '→', startMsg);
    t.sending = true; t.startSentAt = performance.now();
    setStatus(t, 'listening', '');
    // debug.hello arrives right after the connection opens on the test server; without it we are
    // talking to a server that only sends official messages
    t.timers.push(setTimeout(() => {
      if (!t.debug && t.status === 'listening') setStatus(t, 'listening', '호환 모드: 서버의 음성 감지·발화 종료 판단은 표시할 수 없습니다.');
    }, 1000));
  };
  ws.onmessage = (e) => onWsMessage(t, e);
  ws.onerror = () => { t.wsError = true; };
  ws.onclose = (e) => onWsClose(t, e);
}

function onWorklet(t, d) {
  if (d.kind === 'info') { t.contextRate = d.contextRate; return; }
  if (d.kind === 'channels') { t.inChannels = d.channels; return; }
  if (d.kind !== 'frame') return;
  t.framesMade++;
  if (d.frame.byteLength !== FRAME_BYTES) t.badFrames++;
  t.clipIn += d.clipIn; t.clipOut += d.clipOut;
  t.level = Math.max(d.rms, t.level * 0.85);
  if (!t.sending || !t.ws || t.ws.readyState !== WebSocket.OPEN) {
    if (!t.everSent) t.framesBeforeConnect++;
    return;
  }
  const now = performance.now();
  if (t.firstFrameSentAt == null) t.firstFrameSentAt = now;
  t.everSent = true;
  t.ws.send(d.frame);
  t.framesSent++; t.bytesSent += d.frame.byteLength;
  t.maxBuffered = Math.max(t.maxBuffered, t.ws.bufferedAmount);
  if (d.rms >= Number($('vadThr').value || 300)) {
    if (t.firstVoice == null) t.firstVoice = now;
    if (t.micVoiceAt.length < 6000) t.micVoiceAt.push(now);   // for the interruption estimate
    if (!t.eou && t.firstAudioRecvAt == null) t.lastVoice = now;   // browser estimate of the end of speech
  }
  if (t.replay.length < REPLAY_MAX_FRAMES) t.replay.push(d.frame);
}

function onWsMessage(t, e) {
  if (t !== T || t.finalized) return;
  if (typeof e.data !== 'string') return onAudio(t, e.data);
  const now = performance.now();
  let msg;
  try { msg = JSON.parse(e.data); } catch { logEvt(t, '←', `(JSON 아님) ${e.data.slice(0, 200)}`); return; }
  logEvt(t, '←', msg);
  const type = msg && msg.type;
  if (type === 'agent.transcript') {
    t.agentTexts.push(String(msg.text ?? ''));
    $('agentText').textContent = t.agentTexts.join(' '); $('agentText').classList.remove('muted');
  } else if (type === 'turn.end') {
    onTurnEnd(t, now);
  } else if (typeof type === 'string' && type.startsWith('debug.')) {
    t.debug = true;
    onDebug(t, msg, now);
  }
  renderTiming(t);
}

function onAudio(t, ab) {
  const now = performance.now();
  if (t.firstAudioRecvAt == null) {
    t.firstAudioRecvAt = now;
    if (!t.turnEndAt) setStatus(t, 'receiving');
    renderTiming(t);
  }
  t.audioBytesRecv += ab.byteLength; t.audioChunks++;
  if (rmsInt16LE(ab) >= SILENCE_RMS) {         // "voiced" as the starter-kit evaluator counts it
    if (t.firstVoicedRecvAt == null) t.firstVoicedRecvAt = now;
    if (t.lastToolEndAt != null && t.afterToolVoicedAt == null && now > t.lastToolEndAt) t.afterToolVoicedAt = now;
  }
  if (t.turnEndAt) t.audioAfterTurnEnd++;
  try { t.player.push(ab); } catch (e) {
    setPlayback(t, 'error', e.message || String(e));
    logEvt(t, '!', `재생 실패: ${e.message || e}`);
  }
}

function onDebug(t, msg, at) {
  t.dbg.push({ msg, at });
  switch (msg.type) {
    case 'debug.hello':
      t.hello = msg;
      renderServerInfo(t);
      if (t.status === 'listening') setStatus(t, 'listening', '서버 VAD가 음성을 감지하면 상태가 바뀝니다.');
      break;
    case 'debug.tools':
      t.toolsAvail = msg;
      renderTools(t);
      break;
    case 'debug.vad':
      if (msg.event === 'speech_start' && t.status === 'listening') setStatus(t, 'speech');
      if (msg.event === 'resume' && t.status === 'speech') setStatus(t, 'speech', `말이 다시 이어짐 (${msg.resumes}회) — 미리 시작한 STT는 버림`);
      if (msg.event === 'speech_start' && t.userText == null) setUserText(t, '(듣는 중…)', '듣는 중', '');
      if (msg.event === 'resume' && t.userText == null && t.interimText) {
        setUserText(t, t.interimText, '말이 이어져 버린 인식 · 다시 인식 예정', 'interim');
      }
      break;
    case 'debug.stt':
      // speculative STT (customer paused); shown at once, but the turn may still continue
      if (t.userText == null) {
        t.interimText = String(msg.text ?? '');
        setUserText(t, t.interimText || '(인식된 말 없음)', '미리 인식 · 확정 전', 'interim');
      }
      break;
    case 'debug.eou':
      t.eou = { msg, at };
      if (!t.turnEndAt && t.firstAudioRecvAt == null) {
        setStatus(t, 'thinking', `판정 사유 ${msg.reason} · 발화 ${msg.utt_ms} ms · 무음 ${msg.silence_ms} ms`
          + (msg.reuse_speculative_stt ? ' · 미리 실행한 STT 결과 사용' : ''));
      }
      break;
    case 'debug.stt.final':
      t.userText = String(msg.text ?? '');
      setUserText(t, t.userText || '(인식된 말 없음 — 서버가 다시 말해 달라는 응답을 보냄)', '확정 · 응답에 사용', 'final');
      if (lastResult && t.turnEndAt) { lastResult.userText = t.userText; updateTranscriptUser(t); }
      break;
    case 'debug.tool.start':
      t.tools.push({ name: msg.name, args: msg.arguments, startMs: msg.t_ms, startAt: at, end: null });
      renderTools(t);
      break;
    case 'debug.tool.end': {
      const c = t.tools.find((x) => x.name === msg.name && !x.end);
      if (c) { c.end = msg; c.endAt = at; } else t.tools.push({ name: msg.name, args: null, end: msg, endAt: at });
      if (msg.sent) { t.toolSent = true; t.lastToolEndAt = at; t.afterToolVoicedAt = null; }  // last result wins
      renderTools(t);
      break;
    }
    case 'debug.llm.round_end':
      renderTools(t);
      break;
    case 'debug.gate':          // pre-execution gate (llm-gate): blocked calls never reach tool.start
      t.gate.push(msg);
      renderTools(t);
      break;
    case 'debug.blocked_reply':
      t.blocked.push(msg);
      renderTools(t);
      break;
    case 'debug.output_guard':
      t.guardFlags.push(msg);
      renderTools(t);
      break;
    case 'debug.tool_ack':
      t.toolAck = msg;
      renderTools(t);
      break;
    case 'debug.eou_mode':
      t.eouMode = msg.mode;
      renderServerInfo(t);
      break;
    case 'debug.soniox':
      if (msg.event === 'end' && t.status === 'speech') setStatus(t, 'speech', `Soniox 말 끝 신호 수신 · 로컬 무음 ${msg.silence_ms} ms (최소 무음을 채우면 확정)`);
      if (msg.event === 'resume' && t.status === 'speech') setStatus(t, 'speech', 'Soniox 말 끝 신호 뒤 말이 이어짐 — 대기 취소');
      break;
    case 'debug.metrics':
      t.metrics = msg;
      renderAudioInfo(t);
      break;
    case 'debug.mock_calls':
      t.mockCalls = msg;
      renderMockCheck(t);
      maybeFinish(t);
      break;
    default:
      break;
  }
}

function onTurnEnd(t, at) {
  if (t.turnEndAt) return;
  t.turnEndAt = at;
  stopMic(t);                                   // the turn is over: stop sending audio
  lastResult = { userText: t.userText, agentText: t.agentTexts.join(' ') };
  $('btnAddHistory').disabled = false;
  if (!t.debug) { renderTools(t); setUserText(t, '(호환 모드: 대회용 서버는 인식 결과를 보내지 않음 — 테스트 서버 /ws로 연결하면 보임)', '', ''); }
  addTranscript(t);
  addScoreRecord(t, null);
  setStatus(t, 'server_done', t.player.queuedSec() > 0 ? '서버 전송 완료 · 브라우저가 남은 음성을 재생 중' : '서버 전송 완료');
  t.player.end();
  maybeFinish(t);
}

function maybeFinish(t) {
  if (t.finalized || !t.turnEndAt || t.playbackDoneAt == null) return;
  if (t.status !== 'done') setStatus(t, 'done', t.playState === 'none' ? '서버가 음성 없이 turn.end를 보냈습니다.' : '서버 응답 완료 · 브라우저 재생 완료');
  const waitMore = t.debug && !t.mockCalls;
  if (!waitMore) return finalize(t);
  if (!t.finishTimer) { t.finishTimer = setTimeout(() => finalize(t), AFTER_DONE_WAIT_MS); t.timers.push(t.finishTimer); }
}

function finalize(t) { teardown(t, 'done'); }

function onWsClose(t, e) {
  if (t !== T) return;
  renderConn(t);
  if (t.finalized) return;
  if (!t.everSent && !t.startSentAt) {
    return fail(t, `서버에 연결하지 못했습니다 (code ${e.code}). 서버 실행 여부와 URL을 확인하세요.`);
  }
  if (!t.turnEndAt) {
    return fail(t, `turn.end 전에 연결이 끊겼습니다 (code ${e.code}${e.reason ? `, ${e.reason}` : ''}).`
      + (e.code === 1002 ? ' 서버가 session.start 형식을 거부했습니다.' : ''));
  }
  maybeFinish(t);                               // closed after turn.end: let playback finish
}

function fail(t, message) {
  if (t !== T) return;
  t.error = message;
  if (t.everSent && !t.turnEndAt && !t.scored) addScoreRecord(t, '연결 실패·응답 없음');
  setStatus(t, 'error', message);
  logEvt(t, '!', message);
  if (t.player && !t.player.done) setPlayback(t, t.player.started ? 'stopped' : 'idle');
  teardown(t, 'error');
}

function stopTest() {
  const t = T;
  if (!t || t.finalized) return;
  setStatus(t, 'stopped', '사용자가 테스트를 중지했습니다.');
  if (t.player && t.player.started && !t.player.done) setPlayback(t, 'stopped');
  logEvt(t, '!', '사용자 중지');
  teardown(t, 'user stop');
}

function stopMic(t) {
  t.sending = false;
  if (t.micClosed) return;
  t.micClosed = true;
  try { if (t.node) { t.node.port.postMessage('stop'); t.node.port.onmessage = null; t.node.disconnect(); } } catch { /* ignore */ }
  try { if (t.src) t.src.disconnect(); if (t.mute) t.mute.disconnect(); } catch { /* ignore */ }
  if (t.stream) t.stream.getTracks().forEach((x) => { x.onended = null; x.stop(); });
}

function teardown(t, reason) {
  if (t.finalized) return;
  t.finalized = true;
  t.endAt = performance.now();
  for (const id of t.timers) { clearTimeout(id); clearInterval(id); }
  stopMic(t);
  if (t.ws && t.ws.readyState <= WebSocket.OPEN) { try { t.ws.close(1000, reason.slice(0, 60)); } catch { /* ignore */ } }
  if (t.player) t.player.stop();
  if (t.ctx && t.ctx.state !== 'closed') t.ctx.close().catch(() => {}).finally(() => renderAudioInfo(t));
  replayFrames = t.replay.length ? t.replay : replayFrames;
  if (t === T) {
    $('btnStart').disabled = false; $('btnStop').disabled = true; $('btnReplay').disabled = !replayFrames;
    renderConn(t); renderTiming(t); renderAudioInfo(t);
    setTimeout(() => renderConn(t), 300);       // ws.readyState becomes CLOSED asynchronously
  }
}

function tick(t) {
  if (t !== T || t.finalized) return;
  const now = performance.now();
  const thr = Number($('vadThr').value || 300);
  const scale = (v) => Math.min(100, 100 * Math.log10(1 + v) / Math.log10(1 + 32768));
  $('meterBar').style.width = `${t.micClosed ? 0 : scale(t.level)}%`;
  $('meterThr').style.left = `${scale(thr)}%`;
  $('meterVal').textContent = t.micClosed ? '마이크 꺼짐' : String(Math.round(t.level));
  t.level *= 0.9;
  if (t.playState === 'playing') setPlayback(t, 'playing', `남은 ${t.player.queuedSec().toFixed(1)}초`);
  renderConn(t); renderAudioInfo(t);
  if (!t.turnEndAt) {
    const anchor = t.eou ? t.eou.at : t.lastVoice;
    if (anchor != null && now - anchor > TAIL_TIMEOUT_MS) {
      return fail(t, `응답 시간 초과: ${t.eou ? '서버의 발화 종료 확정' : '브라우저가 추정한 발화 끝'} 뒤 ${TAIL_TIMEOUT_MS / 1000}초 안에 turn.end가 오지 않았습니다.`
        + (t.debug && !t.eou ? ' 서버 VAD가 발화를 감지하지 못했을 수 있습니다 (마이크 레벨 확인).' : ''));
    }
    if (now - t.t0 > MAX_TEST_MS) return fail(t, `최대 테스트 시간 ${MAX_TEST_MS / 1000}초를 넘겼습니다.`);
  }
}

// ── what the customer said (server STT) ──
function setUserText(t, text, state, kind) {
  if (t !== T) return;
  const u = $('userText');
  u.textContent = text;
  u.classList.toggle('muted', kind !== 'final');
  const s = $('userTextState');
  s.textContent = state;
  s.className = 'tag' + (kind ? ` ${kind}` : '');
}

// transcript of past tests: page memory only (never stored or sent anywhere)
const transcript = [];
function addTranscript(t) {
  transcript.push({
    sid: t.sid,
    time: new Date().toLocaleTimeString(),
    user: t.debug ? (t.userText ?? t.interimText ?? null) : null,
    agent: t.agentTexts.join(' '),
  });
  renderTranscript();
}
function updateTranscriptUser(t) {           // the final STT text can arrive after turn.end on slow paths
  const e = transcript.find((x) => x.sid === t.sid);
  if (e && t.userText != null && e.user !== t.userText) { e.user = t.userText; renderTranscript(); }
}
function renderTranscript() {
  const ol = $('transcript');
  ol.textContent = '';
  if (!transcript.length) { ol.appendChild(el('li', 'muted', '아직 없음')); return; }
  for (const e of transcript) {
    const li = el('li');
    li.appendChild(el('span', 'detail', `${e.time} · ${e.sid}`));
    const u = el('span', 'line'); u.appendChild(el('b', null, '고객:'));
    u.appendChild(document.createTextNode(e.user == null ? '(인식 결과 없음 · 호환 모드)' : (e.user || '(인식된 말 없음)')));
    const a = el('span', 'line'); a.appendChild(el('b', null, '상담원:'));
    a.appendChild(document.createTextNode(e.agent || '(텍스트 없음)'));
    li.append(u, a);
    ol.appendChild(li);
  }
  ol.scrollTop = ol.scrollHeight;
}
function transcriptText() {
  return transcript.map((e) => `[${e.time} ${e.sid}]\n고객: ${e.user ?? '(인식 결과 없음)'}\n상담원: ${e.agent}`).join('\n\n');
}

// ── practice score (page memory only) ──
const scores = [];
function addScoreRecord(t, failReason) {
  if (t.scored) return;
  t.scored = true;
  // reference point = end of the customer's speech. Debug mode: the server VAD's last voiced frame, moved to the
  // browser clock through the debug.eou receipt (local link: transit ~0). Compatibility mode: browser estimate.
  const e = t.eou && t.eou.msg;
  const anchor = e && e.t_commit_ms != null && e.t_last_speech_ms != null
    ? t.eou.at - (e.t_commit_ms - e.t_last_speech_ms) : t.lastVoice;
  const firstSound = t.firstVoicedRecvAt != null && anchor != null ? (t.firstVoicedRecvAt - anchor) / 1000 : null;
  const afterTool = t.toolSent && t.afterToolVoicedAt != null ? (t.afterToolVoicedAt - t.lastToolEndAt) / 1000 : null;
  // interruption estimate: the mic still heard speech when the first agent sound arrived (before it is played,
  // so the agent's own voice cannot be the cause), or the sound came before the reference point
  const f = t.firstVoicedRecvAt;
  const talking = f != null && t.micVoiceAt.some((x) => x > f - 200 && x < f + 100);
  const zeroReason = failReason || (talking || (firstSound != null && firstSound < 0) ? '끼어듦 의심 (브라우저 추정)' : null);
  const sent = t.tools.filter((c) => c.end && c.end.sent).map((c) => `${c.name} ${c.args ?? ''}`.trim());
  const blocked = t.gate.flatMap((g) => (g.decisions || []).filter((d) => !d.allowed).map((d) => `${d.name} (관문 차단, 보내지 않음)`));
  scores.push({
    n: scores.length + 1, sid: t.sid, user: t.debug ? (t.userText ?? '') : null, compat: !t.debug,
    firstSound, afterTool, toolCalled: t.toolSent, calls: sent, blocked,
    ...caseSpeed({ firstSound, afterTool, toolCalled: t.toolSent, zeroReason }),
    response: null, func: null,
  });
  renderScores();
}

const fmtS = (v) => (v == null ? '—' : `${v.toFixed(2)}초`);
const fmtN = (v, d = 1) => (v == null ? '—' : (Number.isInteger(v) ? String(v) : v.toFixed(d)));

function judgeSelect(rec, key, options) {
  const sel = el('select');
  sel.appendChild(new Option('판정 전', ''));
  for (const [v, label] of options) sel.appendChild(new Option(label, String(v)));
  sel.value = rec[key] == null ? '' : String(rec[key]);
  sel.onchange = () => { rec[key] = parseJudgment(sel.value); renderScores(); };
  return sel;
}

function renderScores() {
  const tb = document.querySelector('#scoreTable tbody'), tf = document.querySelector('#scoreTable tfoot');
  tb.textContent = ''; tf.textContent = '';
  if (!scores.length) {
    $('scoreNow').textContent = '테스트를 마치면 속도 점수가 자동으로 계산됩니다.';
    $('scoreNow').className = 'score-now muted';
    return;
  }
  for (const r of scores) {
    const sys = systemScore(r);
    const tr = document.createElement('tr');
    tr.appendChild(el('td', 'num', String(r.n)));
    const said = el('td', 'said', r.user == null ? '(호환 모드)' : (r.user || '(인식된 말 없음)'));
    said.title = r.sid;
    tr.appendChild(said);
    tr.appendChild(el('td', 'num', r.firstSound == null ? '—' : `${fmtS(r.firstSound)} → ${r.first}`));
    tr.appendChild(el('td', 'num', r.compat ? '알 수 없음' : (r.toolCalled ? `${fmtS(r.afterTool)} → ${r.after}` : '함수 없음')));
    const sp = el('td', 'num' + (r.reason ? ' err' : ''), fmtN(r.speed));
    if (r.reason) sp.appendChild(el('div', 'detail', r.reason));
    tr.appendChild(sp);
    const rc = el('td'); rc.appendChild(judgeSelect(r, 'response', RESPONSE_OPTIONS)); tr.appendChild(rc);
    const fc = el('td'); fc.appendChild(judgeSelect(r, 'func', FUNC_OPTIONS));
    fc.appendChild(el('div', 'detail', r.compat ? '호환 모드: 호출 정보 없음'
      : [...r.calls, ...r.blocked].join(' · ') || '보낸 호출 없음 (호출하면 안 되는 문항이면 맞음)'));
    tr.appendChild(fc);
    tr.appendChild(el('td', 'num', sys.total == null ? `속도분 ${fmtN(sys.speedPart)} / 30` : fmtN(sys.total)));
    tr.appendChild(el('td', 'num strong', sys.item20 == null ? '—' : fmtN(sys.item20, 2)));
    tb.appendChild(tr);
  }
  const avg = averageScores(scores);
  const fr = document.createElement('tr');
  fr.appendChild(el('td', null, '평균'));
  fr.appendChild(el('td', 'detail', `${avg.n}건 중 판정 완료 ${avg.judged}건`));
  fr.appendChild(el('td')); fr.appendChild(el('td'));
  fr.appendChild(el('td', 'num', fmtN(avg.speed)));
  fr.appendChild(el('td')); fr.appendChild(el('td'));
  fr.appendChild(el('td', 'num', fmtN(avg.total)));
  fr.appendChild(el('td', 'num strong', avg.item20 == null ? '—' : `${fmtN(avg.item20, 2)} / 20`));
  tf.appendChild(fr);

  const last = scores.at(-1);
  const parts = [`최근 테스트 #${last.n}: 첫 소리 ${fmtS(last.firstSound)} (${last.first}점)`];
  if (last.toolCalled) parts.push(`조회 뒤 답 ${fmtS(last.afterTool)} (${last.after}점)`);
  parts.push(`속도 점수 ${fmtN(last.speed)}${last.reason ? ` — ${last.reason}` : ''}`);
  const ok = last.speed > 0;
  parts.push(ok ? '득점 가능 구간 (6초 이하)' : '이 문항 속도 0점');
  $('scoreNow').textContent = parts.join(' · ');
  $('scoreNow').className = 'score-now ' + (ok ? 'ok' : 'err');
}

function renderSpeedTable() {
  const tb = document.querySelector('#speedTable tbody');
  const rowOf = (a, b) => { const tr = document.createElement('tr'); tr.append(el('td', null, a), el('td', 'num', String(b))); return tr; };
  for (const [bound, pts] of SPEED_TABLE) tb.appendChild(rowOf(`${bound.toFixed(1)}초 이하`, pts));
  tb.appendChild(rowOf(`${SPEED_TABLE.at(-1)[0].toFixed(1)}초 초과`, 0));
}
renderSpeedTable();

// ── rendering ──
function resetView(t) {
  $('log').textContent = '';
  setUserText(t, '-', '', '');
  $('agentText').textContent = '-'; $('agentText').classList.add('muted');
  $('tools').textContent = '-'; $('tools').className = 'muted';
  $('mockCheck').textContent = '';
  $('sttNote').textContent = '';
  $('serverInfo').textContent = '-';
  setStatus(t, 'idle'); setPlayback(t, 'idle');
  renderTiming(t); renderAudioInfo(t); renderConn(t);
}

function renderConn(t) {
  if (t !== T) return;
  const ws = t.ws;
  const states = ['연결 중', '연결됨', '닫는 중', '닫힘'];
  $('conn').textContent = ws ? `${states[ws.readyState]} · ${ws.url}${t.debug ? ' · 디버그 이벤트 수신' : ' · 공식 메시지만'}` : '-';
}

function renderServerInfo(t) {
  const h = t.hello;
  if (!h) return;
  const c = h.config || {}, p = h.providers || {};
  $('serverInfo').textContent = `STT ${p.stt}/${c.stt_model} · LLM ${p.llm}/${c.llm_model} · TTS ${p.tts}/${c.tts_model}`
    + (c.eou_mode === 'soniox_stream'
      ? ` · 발화 종료: Soniox 스트리밍 <end> (최대 지연 ${c.soniox_max_endpoint_delay_ms}ms, 최소 무음 ${c.soniox_min_silence_ms}ms, 대체 ${c.soniox_fallback_silence_ms}ms)`
        + (t.eouMode && t.eouMode !== 'soniox_stream' ? ` → 이번 세션은 ${t.eouMode}` : '')
      : ` · 발화 종료: 완결 ${c.complete_silence_ms}ms / 최대 ${c.max_silence_ms}ms (VAD ${c.vad}, 에너지 하한 ${c.energy_min_rms})`)
    + ` · 함수 관문 ${c.tool_gate ? '켬' : '끔'} · 출력 검사 ${c.output_guard === 'log' ? 'log(기록만, 반복 문장은 버림)' : c.output_guard}`
    + ` · 조회 중 멘트 ${c.tool_ack ? '켬' : '끔'} · 첫 조각 쉼표 끊기 ${c.first_chunk_min_chars ? c.first_chunk_min_chars + '자' : '끔'}`;
  if (p.llm === 'scripted' || p.tts === 'tone' || p.stt === 'fake') {
    $('sttNote').textContent = '주의: 서버가 테스트 대체물(scripted/tone/fake)로 실행 중입니다. AI 응답이 아닙니다.'
      + (p.stt === 'fake' ? ' STT가 fake라서 마이크 소리를 인식하지 않고 AGENT_STT_FAKE_TEXT 문장(비어 있으면 빈 문장)을 돌려줍니다.'
        + ' 실제로 인식하려면 --stt faster_whisper(로컬, 키 없음) 또는 --stt openai(키 필요)로 서버를 다시 띄우세요.' : '');
  }
}

function renderTools(t) {
  const box = $('tools');
  box.textContent = ''; box.className = '';
  if (!t.debug) { box.className = 'muted'; box.textContent = '호환 모드에서는 함수 호출 정보를 받을 수 없습니다.'; return; }
  const avail = t.toolsAvail;
  box.appendChild(el('p', 'detail', avail ? (avail.error ? `함수 목록 조회 실패: ${avail.error}` : `사용 가능 함수: ${avail.names.join(', ') || '없음'}`) : '함수 목록 조회 중'));
  const rounds = t.dbg.filter((d) => d.msg.type === 'debug.llm.round_end');
  if (rounds.length) {
    box.appendChild(el('p', 'detail', rounds.map((r) => `LLM 라운드 ${r.msg.round}: ${r.msg.tool_calls.length ? '함수 요청 ' + r.msg.tool_calls.map((c) => c.name).join(', ') : '응답 문장'}`).join(' · ')));
  }
  if (t.toolAck) box.appendChild(el('p', 'detail', `조회 중 멘트 재생: "${t.toolAck.text}" (미리 합성한 음성)`));
  for (const g of t.gate) {
    for (const d of g.decisions || []) {
      if (d.allowed) continue;   // allowed calls appear below as tool.start / tool.end
      const box2 = el('div', 'tool');
      box2.appendChild(el('b', 'err', `${d.name} · 실행 전 관문에서 차단 (${GATE_REASONS[d.reason] || d.reason}) · 함수 서버로 보내지 않음`));
      box2.appendChild(el('div', 'detail', '모델이 요청한 인자 (내부 동의 칸 제외)'));
      box2.appendChild(el('pre', 'mono', pretty(d.args)));
      box2.appendChild(el('div', 'detail', `모델에게 돌려준 안내: ${d.message}`));
      box.appendChild(box2);
    }
  }
  for (const b of t.blocked) {
    box.appendChild(el('p', 'detail', `차단 뒤 응답 방식: ${BLOCKED_HOW[b.how] || b.how}` + (b.sentence ? ` — "${b.sentence}"` : '')));
  }
  for (const f of t.guardFlags) {
    const fl = f.flag || {};
    const what = fl.kind === 'repeated_sentence' ? '같은 턴에서 이미 말한 문장 → 버림'
      : fl.kind === 'ungrounded_number' ? `근거 없는 숫자 ${(fl.values || []).join(', ')} → ${fl.action === 'block' ? '대체 문장으로 바꿈' : '기록만 (문장은 그대로 말함)'}`
      : JSON.stringify(fl);
    box.appendChild(el('p', 'warn', `출력 검사: ${what} · 문장 "${f.sentence}"`));
  }
  if (!t.tools.length) {
    if (!t.gate.length) box.appendChild(el('p', 'muted', '이번 턴에는 함수 호출이 없습니다.'));
    return;
  }
  for (const c of t.tools) {
    const d = el('div', 'tool');
    const r = c.end && c.end.result;
    const isErr = c.end && (!c.end.sent || (r && r.status === 'error') || (c.end.http_status >= 400));
    let head = c.name;
    if (!c.end) head += ' · 진행 중';
    else head += ` · ${c.end.ms} ms` + (c.end.http_status ? ` · HTTP ${c.end.http_status}` : '') + (c.end.sent ? '' : ' · 함수 서버로 보내지 않음 (ToolsClient 검사: 필수 인자 누락·정의에 없는 함수 등)');
    d.appendChild(el('b', isErr ? 'err' : '', head));
    d.appendChild(el('div', 'detail', '인자'));
    d.appendChild(el('pre', 'mono', c.args == null ? '-' : pretty(c.args)));
    if (c.end) { d.appendChild(el('div', 'detail', '결과')); d.appendChild(el('pre', 'mono' + (isErr ? ' err' : ''), pretty(r))); }
    box.appendChild(d);
  }
}

function renderMockCheck(t) {
  const box = $('mockCheck');
  const m = t.mockCalls;
  if (!m) { box.textContent = ''; return; }
  if (m.error) { box.textContent = `함수 서버 기록(/calls)을 확인할 수 없음: ${m.error}`; return; }
  const recorded = (m.calls || []).map((x) => `${x.name} ${canon(x.arguments)}`).sort();
  const sent = t.tools.filter((c) => c.end && c.end.sent).map((c) => `${c.name} ${canon(c.args)}`).sort();
  const same = recorded.length === sent.length && recorded.every((v, i) => v === sent[i]);
  const matched = (m.calls || []).map((x) => `${x.name}: matched=${x.matched}`).join(', ');
  box.textContent = `함수 서버 기록 ${recorded.length}건 / 상담원이 보낸 호출 ${sent.length}건 — ${same ? '일치' : '불일치'}`
    + (matched ? ` (${matched})` : '');
}

function renderTiming(t) {
  const tb = document.querySelector('#timing tbody');
  tb.textContent = '';
  const fmt = (v) => (v == null || Number.isNaN(v)) ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v)} ms`;
  const aS = t.eou ? t.eou.msg.t_last_speech_ms : null;      // server anchor (server clock)
  const aB = t.lastVoice;                                     // browser anchor (estimate)
  const sv = (ms) => (aS != null && ms != null) ? ms - aS : null;
  const bv = (at) => (aB != null && at != null) ? at - aB : null;
  const first = (type, pred = () => true) => t.dbg.find((d) => d.msg.type === type && pred(d.msg));
  const vad = first('debug.vad', (m) => m.event === 'speech_start');
  const finalAt = first('debug.stt.final');
  const sttUsed = finalAt ? [...t.dbg].reverse().find((d) => d.msg.type === 'debug.stt' && d.msg.t_ms <= finalAt.msg.t_ms) : null;
  const llm1 = first('debug.llm.first_text');
  const sent1 = first('debug.audio.first_sent');
  const tend = first('debug.turn_end_sent');
  const rows = [
    ['마이크 → 서버 전송 시작', null, bv(t.firstFrameSentAt)],
    ['음성 시작 (서버 VAD / 브라우저 추정)', sv(vad && vad.msg.t_ms), bv(t.firstVoice)],
    ['마지막 음성 = 기준점 (서버 VAD / 브라우저 추정)', aS != null ? 0 : null, aB != null ? 0 : null],
    ...(first('debug.soniox', (m) => m.event === 'end') ? [['Soniox 말 끝 신호(<end>) 수신', sv(first('debug.soniox', (m) => m.event === 'end').msg.t_ms), bv(first('debug.soniox', (m) => m.event === 'end').at)]] : []),
    [`발화 종료 확정${t.eou ? ` (${t.eou.msg.reason})` : ''} · 브라우저는 수신 시각`, sv(t.eou && t.eou.msg.t_commit_ms), bv(t.eou && t.eou.at)],
    [`STT 완료${sttUsed ? ` (${sttUsed.msg.ms} ms 소요)` : ''}`, sv(sttUsed && sttUsed.msg.t_ms), bv(sttUsed && sttUsed.at)],
    ['LLM 첫 텍스트', sv(llm1 && llm1.msg.t_ms), bv(llm1 && llm1.at)],
  ];
  t.tools.forEach((c, i) => {
    rows.push([`함수 ${i + 1} ${c.name} 시작`, sv(c.startMs), bv(c.startAt)]);
    if (c.end) rows.push([`함수 ${i + 1} ${c.name} 완료`, sv(c.end.t_ms), bv(c.endAt)]);
  });
  rows.push(
    ['첫 음성 데이터 (서버 전송 / 브라우저 수신)', sv(sent1 && sent1.msg.t_ms), bv(t.firstAudioRecvAt)],
    ['브라우저 첫 재생 시작 (예약 시각 + 출력 지연 추정)', null, bv(t.firstPlayAt)],
    ['turn.end (서버 전송 / 브라우저 수신)', sv(tend && tend.msg.t_ms), bv(t.turnEndAt)],
    ['브라우저 재생 완료', null, bv(t.playbackDoneAt)],
  );
  for (const [label, s, b] of rows) {
    const tr = document.createElement('tr');
    tr.appendChild(el('td', null, label));
    tr.appendChild(el('td', 'num', t.debug || s != null ? fmt(s) : '제공 안 됨'));
    tr.appendChild(el('td', 'num', fmt(b) + (b != null && aB != null ? ' (추정)' : '')));
    tb.appendChild(tr);
  }
  const sum = document.createElement('tr');
  sum.appendChild(el('td', null, '요약: 마지막 음성 → 첫 응답 소리'));
  sum.appendChild(el('td', 'num', fmt(sv(sent1 && sent1.msg.t_ms)) + (sent1 ? ' (서버 전송)' : '')));
  sum.appendChild(el('td', 'num', fmt(bv(t.firstPlayAt)) + (t.firstPlayAt != null && aB != null ? ' (체감, 추정)' : '')));
  sum.style.fontWeight = '600';
  tb.appendChild(sum);
}

function renderAudioInfo(t) {
  if (t !== T) return;
  const dl = $('audioInfo');
  dl.textContent = '';
  const add = (k, v) => { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); };
  const s = t.trackSettings || {};
  const elapsed = t.firstFrameSentAt != null ? ((t.turnEndAt || (t.finalized ? t.endAt : null) || performance.now()) - t.firstFrameSentAt) / 1000 : 0;
  const sentSec = t.framesSent * FRAME_SAMPLES / TARGET_RATE;
  add('사용 중인 마이크', t.track ? `${t.track.label || '(이름 없음)'}${t.track.muted ? ' · 음소거 상태' : ''}` : '-');
  add('입력 장치 (브라우저 보고)', s.deviceId ? `${s.sampleRate ? s.sampleRate + ' Hz' : '샘플레이트 미보고'} · ${s.channelCount ? s.channelCount + 'ch' : '채널 미보고'} · 에코제거 ${s.echoCancellation} · 잡음억제 ${s.noiseSuppression} · 자동음량 ${s.autoGainControl}` : '-');
  add('변환 전 (실측)', t.contextRate ? `${t.contextRate} Hz · ${t.inChannels || '?'}ch → 16000 Hz mono` : '-');
  add('전송 형식', `pcm_s16le · 16000 Hz · mono · ${FRAME_BYTES} B / 20 ms`);
  add('보낸 프레임', `${t.framesSent}개 (${t.bytesSent} B) · 크기 이상 ${t.badFrames}건 · 연결 전 버림 ${t.framesBeforeConnect}개`);
  add('보낸 길이 / 경과 시간', t.framesSent ? `${sentSec.toFixed(2)}초 / ${elapsed.toFixed(2)}초 (비율 ${(sentSec / Math.max(elapsed, 0.001)).toFixed(3)})` : '-');
  add('클리핑 샘플', `입력 ${t.clipIn} · 변환 후 ${t.clipOut}`);
  add('송신 대기열 최대', `${t.maxBuffered} B`);
  add('받은 음성', t.audioChunks ? `${(t.audioBytesRecv / 2 / TARGET_RATE).toFixed(2)}초 · ${t.audioChunks}조각 · turn.end 뒤 ${t.audioAfterTurnEnd}조각` : '-');
  add('재생 끊김 (큐 비어 다시 시작)', t.player ? `${t.player.gaps}회` : '-');
  if (t.metrics && t.metrics.row) {
    const r = t.metrics.row;
    add('서버 기록 (metrics)', `outcome ${r.outcome} · 발화 ${r.utt_ms} ms · 재개 ${r.resumes}회 · 응답 중 무시한 프레임 ${r.ignored_frames_while_responding}`);
  }
  add('자원 상태', `마이크 ${t.track ? t.track.readyState : '-'} · WebSocket ${t.ws ? ['연결 중', '열림', '닫는 중', '닫힘'][t.ws.readyState] : '-'} · 오디오 ${t.ctx ? t.ctx.state : '-'}`);
}

// ── buttons ──
function addHistory() {
  if (!lastResult) return;
  let h;
  try { h = JSON.parse($('history').value.trim() || '[]'); if (!Array.isArray(h)) throw new Error(); }
  catch { $('statusDetail').textContent = 'history가 JSON 배열이 아니어서 추가하지 못했습니다.'; return; }
  h.push({ role: 'user', text: lastResult.userText ?? '' }, { role: 'assistant', text: lastResult.agentText });
  $('history').value = JSON.stringify(h, null, 2);
  $('useHistory').checked = true;
  $('btnAddHistory').disabled = true;
  if (lastResult.userText == null) $('statusDetail').textContent = '고객 발화 텍스트를 알 수 없어 빈 칸으로 넣었습니다 (호환 모드). history에서 직접 입력하세요.';
}

async function replay() {
  if (!replayFrames || !replayFrames.length) return;
  try {
    if (replayCtx) await replayCtx.close().catch(() => {});
    replayCtx = new AudioContext();
    const buf = replayCtx.createBuffer(1, replayFrames.length * FRAME_SAMPLES, TARGET_RATE);
    const ch = buf.getChannelData(0);
    let o = 0;
    for (const f of replayFrames) {
      const dv = new DataView(f);
      for (let i = 0; i < FRAME_SAMPLES; i++) ch[o++] = dv.getInt16(i * 2, true) / 32768;
    }
    const src = replayCtx.createBufferSource();
    src.buffer = buf; src.connect(replayCtx.destination); src.start();
    const ctx = replayCtx;
    src.onended = () => { ctx.close().catch(() => {}); if (replayCtx === ctx) replayCtx = null; };
  } catch (e) { $('statusDetail').textContent = `다시 듣기 실패: ${e.message || e}`; }
}

// ── microphone device choice and a level check that does not touch the server ──
function micConstraints() {
  const id = $('micDevice').value;
  return {
    ...(id ? { deviceId: { exact: id } } : {}),
    channelCount: { ideal: 1 }, echoCancellation: $('ec').checked,
    noiseSuppression: $('ns').checked, autoGainControl: $('agc').checked,
  };
}

function savedMicId() {
  try { return JSON.parse(localStorage.getItem('devui.settings') || '{}').micDevice || ''; } catch { return ''; }
}

async function refreshDevices() {
  const sel = $('micDevice');
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  let devs = [];
  try { devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput'); } catch { return; }
  const keep = sel.value || savedMicId();
  sel.textContent = '';
  sel.appendChild(new Option('시스템 기본 장치', ''));
  devs.forEach((d, i) => {
    if (d.deviceId === 'default' || d.deviceId === 'communications') return;   // aliases of real devices
    sel.appendChild(new Option(d.label || `마이크 ${i + 1} (이름은 권한 허용 후 표시)`, d.deviceId));
  });
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  if (!devs.length) $('micCheck').textContent = '브라우저가 찾은 마이크가 없습니다. 장치 연결과 Windows 개인정보(마이크) 설정을 확인하세요.';
}

let micChecking = false;
async function micCheck() {
  if (micChecking) return;
  if (T && !T.finalized) { $('micCheck').textContent = '테스트 중에는 점검할 수 없습니다. 테스트를 끝낸 뒤 누르세요.'; return; }
  micChecking = true;
  const out = $('micCheck');
  const btn = $('btnMicCheck');
  btn.disabled = true;
  let stream = null, ctx = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    const track = stream.getAudioTracks()[0];
    const st = track.getSettings ? track.getSettings() : {};
    await refreshDevices();
    ctx = new AudioContext();
    await ctx.resume();
    const an = ctx.createAnalyser();
    an.fftSize = 2048;
    ctx.createMediaStreamSource(stream).connect(an);      // analysed only, never played
    const buf = new Float32Array(an.fftSize);
    const level = () => {
      an.getFloatTimeDomainData(buf);
      let s = 0;
      for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
      return Math.sqrt(s / buf.length) * 32768;            // int16 RMS units, like the level meter
    };
    const listen = async (ms, label) => {
      const xs = [];
      const t0 = performance.now();
      while (performance.now() - t0 < ms) {
        await new Promise((r) => setTimeout(r, 50));
        const v = level();
        xs.push(v);
        out.textContent = `"${track.label || '이름 없음'}" · ${label} · 현재 레벨 ${Math.round(v)}`;
      }
      xs.sort((a, b) => a - b);
      return { median: xs[Math.floor(xs.length / 2)] || 0, peak: xs.at(-1) || 0 };
    };
    const quiet = await listen(1500, '1단계: 1.5초 동안 조용히 있어 주세요');
    const voice = await listen(2500, '2단계: 지금 "주문번호 데모 일공공일" 하고 말해 보세요');
    const thr = Number($('vadThr').value || 300);
    let verdict, ok = false;
    if (track.muted) verdict = '장치가 음소거 상태입니다. 장치나 Windows 소리 설정의 음소거를 확인하세요.';
    else if (voice.peak < 30) verdict = '소리가 거의 들어오지 않습니다. 연결이 안 된 장치이거나 음소거입니다. 다른 마이크를 선택하세요.';
    else if (voice.peak < quiet.median * 2) verdict = '말할 때와 조용할 때 레벨 차이가 거의 없습니다. 잡음만 들어오고 있어 이 장치는 실제 마이크가 아닐 가능성이 큽니다. 다른 마이크를 선택하세요.';
    else if (voice.peak < thr) verdict = `목소리는 들어오지만 작습니다 (기준 ${thr} 미만). 마이크에 가까이 말하거나 Windows 입력 음량을 올리세요.`;
    else { verdict = '정상: 목소리가 잡음과 구분됩니다. 이 장치로 테스트하세요.'; ok = true; }
    out.textContent = `장치 "${track.label || '이름 없음'}"${st.sampleRate ? ` · ${st.sampleRate} Hz` : ''} · 조용할 때 ${Math.round(quiet.median)}`
      + ` · 말할 때 최대 ${Math.round(voice.peak)} → ${verdict}`;
    out.className = ok ? 'detail' : 'warn';
  } catch (e) {
    out.textContent = micErrorText(e);
    out.className = 'warn';
  } finally {
    if (stream) stream.getTracks().forEach((x) => x.stop());
    if (ctx) ctx.close().catch(() => {});
    btn.disabled = false;
    micChecking = false;
  }
}

$('btnMicCheck').onclick = micCheck;
$('btnMicRefresh').onclick = refreshDevices;
$('micDevice').onchange = saveSettings;
if (navigator.mediaDevices && 'ondevicechange' in navigator.mediaDevices) navigator.mediaDevices.ondevicechange = refreshDevices;
refreshDevices();

$('btnStart').onclick = () => { startTest().catch((e) => T && fail(T, `예상하지 못한 오류: ${e.message || e}`)); };
$('btnStop').onclick = stopTest;
$('btnReplay').onclick = replay;
$('btnAddHistory').onclick = addHistory;
$('btnClearHistory').onclick = () => { $('history').value = '[]'; };
$('btnClearTranscript').onclick = () => { transcript.length = 0; renderTranscript(); scores.length = 0; renderScores(); };
$('btnCopyTranscript').onclick = async () => {
  try { await navigator.clipboard.writeText(transcriptText()); $('statusDetail').textContent = '대화 기록을 클립보드에 복사했습니다.'; }
  catch { $('statusDetail').textContent = '클립보드 복사가 막혀 있습니다. 브라우저 권한을 확인하세요.'; }
};
$('mode').onchange = () => { $('wsUrl').value = defaultWsUrl($('mode').value); saveSettings(); };
window.addEventListener('pagehide', () => { if (T && !T.finalized) teardown(T, 'page closed'); });
$('now').value = nowString();
loadSettings();
