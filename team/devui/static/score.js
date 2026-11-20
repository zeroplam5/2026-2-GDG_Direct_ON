// Practice score calculator for the voice test page (pure functions, tested in tests/score.test.mjs).
// Follows the published system-score rules (2026 DIRECT:ON): response 50% + function 20% + speed 30%,
// system total (0-100) / 5 = the 20-point item. Response and function are JUDGED BY A PERSON on this page
// (no answer key here); only speed is measured, and in the browser it is an ESTIMATE, not the official value.

export const WEIGHTS = { response: 50, func: 20, speed: 30 };

// seconds -> points (same table as eval/score_demo.py)
export const SPEED_TABLE = [[1.5, 100], [2.0, 90], [2.5, 80], [3.0, 70], [3.5, 60], [4.0, 50], [5.0, 30], [6.0, 10]];

export const RESPONSE_OPTIONS = [[1, '맞음 1.0'], [0.7, '괜찮음 0.7'], [0, '틀림 0']];
export const FUNC_OPTIONS = [[1, '맞음 1'], [0, '틀림 0']];

// the starter-kit evaluator treats an agent frame below this RMS as silence (eval/run_eval.py SILENCE_RMS)
export const SILENCE_RMS = 300;

export function speedPoints(sec) {
  if (sec == null || typeof sec !== 'number' || !Number.isFinite(sec) || sec < 0) return 0;
  for (const [bound, pts] of SPEED_TABLE) if (sec <= bound) return pts;
  return 0;
}

/**
 * Speed score of one case.
 * firstSound: s from end of customer speech to the first voiced agent frame (null = no sound)
 * afterTool:  s from the last function result to the next voiced frame (null when no function was called,
 *             or when the agent said nothing after the result)
 * toolCalled: a function call actually reached the function server in this case
 * zeroReason: interruption / no sound / connection failure -> 0 for the case
 */
export function caseSpeed({ firstSound = null, afterTool = null, toolCalled = false, zeroReason = null } = {}) {
  if (zeroReason) return { first: 0, after: toolCalled ? 0 : null, speed: 0, reason: zeroReason };
  if (firstSound == null) return { first: 0, after: toolCalled ? 0 : null, speed: 0, reason: '소리 없음' };
  const first = speedPoints(firstSound);
  if (!toolCalled) return { first, after: null, speed: first, reason: null };
  const after = speedPoints(afterTool);   // no answer after the result -> 0
  return { first, after, speed: (first + after) / 2, reason: afterTool == null ? '조회 뒤 답 없음' : null };
}

/**
 * System score of one case. response: 1 | 0.7 | 0 | null, func: 1 | 0 | null, speed: 0-100.
 * Returns total (0-100) and item20 (total / 5) only when both judgments are given; otherwise the
 * part that is known so far (speedPart, out of 30).
 */
export function systemScore({ response = null, func = null, speed = 0 } = {}) {
  const speedPart = WEIGHTS.speed * speed / 100;
  if (response == null || func == null) return { total: null, item20: null, speedPart };
  const total = WEIGHTS.response * response + WEIGHTS.func * func + speedPart;
  return { total, item20: total / 5, speedPart };
}

/** Average over cases: speed over every case, total / item20 over the fully judged ones. */
export function averageScores(cases) {
  const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const judged = cases.map((c) => systemScore(c)).filter((s) => s.total != null);
  return {
    n: cases.length,
    judged: judged.length,
    speed: mean(cases.map((c) => c.speed)),
    total: mean(judged.map((s) => s.total)),
    item20: mean(judged.map((s) => s.item20)),
  };
}

export function parseJudgment(v) {
  if (v === '' || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
