// Voice test page: mic -> 16 kHz s16le mono 640-byte frames -> WebSocket; server PCM -> ordered playback.
// Only states the server actually reports are shown as facts; browser-side estimates are labelled.
import { StreamResampler, S16Decoder, FRAME_BYTES, FRAME_SAMPLES, TARGET_RATE } from './pcm.js';

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
const PERSIST = ['mode', 'wsUrl', 'toolsUrl', 'vadThr', 'ec', 'ns', 'agc', 'useHistory'];
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
    firstAudioRecvAt: null, firstPlayAt: null, turnEndAt: null, playbackDoneAt: null,
    audioBytesRecv: 0, audioChunks: 0, audioAfterTurnEnd: 0,
  };
}

function micErrorText(e) {
  switch (e && e.name) {
    case 'NotAllowedError': case 'SecurityError':
      return '마이크 권한이 거부되었습니다. 주소창의 사이트 설정에서 마이크를 허용한 뒤 다시 시작하세요.';
    case 'NotFoundError': case 'OverconstrainedError': return '사용할 수 있는 마이크 장치가 없습니다.';
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
    t.stream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: { ideal: 1 }, echoCancellation: $('ec').checked,
      noiseSuppression: $('ns').checked, autoGainControl: $('agc').checked } });
  } catch (e) { return fail(t, micErrorText(e)); }
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
      renderTools(t);
      break;
    }
    case 'debug.llm.round_end':
      renderTools(t);
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
    + ` · 발화 종료: 완결 ${c.complete_silence_ms}ms / 최대 ${c.max_silence_ms}ms (VAD ${c.vad}, 에너지 하한 ${c.energy_min_rms})`;
  if (p.llm === 'scripted' || p.tts === 'tone' || p.stt === 'fake') {
    $('sttNote').textContent = '주의: 서버가 테스트 대체물(scripted/tone/fake)로 실행 중입니다. AI 응답이 아닙니다.';
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
  if (!t.tools.length) { box.appendChild(el('p', 'muted', '이번 턴에는 함수 호출이 없습니다.')); return; }
  for (const c of t.tools) {
    const d = el('div', 'tool');
    const r = c.end && c.end.result;
    const isErr = c.end && (!c.end.sent || (r && r.status === 'error') || (c.end.http_status >= 400));
    let head = c.name;
    if (!c.end) head += ' · 진행 중';
    else head += ` · ${c.end.ms} ms` + (c.end.http_status ? ` · HTTP ${c.end.http_status}` : '') + (c.end.sent ? '' : ' · 함수 서버로 보내지 않음 (서버 사전 검증에서 차단)');
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

$('btnStart').onclick = () => { startTest().catch((e) => T && fail(T, `예상하지 못한 오류: ${e.message || e}`)); };
$('btnStop').onclick = stopTest;
$('btnReplay').onclick = replay;
$('btnAddHistory').onclick = addHistory;
$('btnClearHistory').onclick = () => { $('history').value = '[]'; };
$('btnClearTranscript').onclick = () => { transcript.length = 0; renderTranscript(); };
$('btnCopyTranscript').onclick = async () => {
  try { await navigator.clipboard.writeText(transcriptText()); $('statusDetail').textContent = '대화 기록을 클립보드에 복사했습니다.'; }
  catch { $('statusDetail').textContent = '클립보드 복사가 막혀 있습니다. 브라우저 권한을 확인하세요.'; }
};
$('mode').onchange = () => { $('wsUrl').value = defaultWsUrl($('mode').value); saveSettings(); };
window.addEventListener('pagehide', () => { if (T && !T.finalized) teardown(T, 'page closed'); });
$('now').value = nowString();
loadSettings();
