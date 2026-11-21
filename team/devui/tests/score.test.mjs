// Node tests for the practice score calculator (run: node --test team/devui/tests/)
import test from 'node:test';
import assert from 'node:assert/strict';
import { speedPoints, caseSpeed, systemScore, averageScores, parseJudgment } from '../static/score.js';

test('speed table boundaries match the published table', () => {
  const cases = [[0.9, 100], [1.5, 100], [1.51, 90], [2.0, 90], [2.5, 80], [3.0, 70], [3.5, 60], [4.0, 50],
    [4.5, 30], [5.0, 30], [5.5, 10], [6.0, 10], [6.01, 0], [null, 0], [-1, 0], [NaN, 0]];
  for (const [s, p] of cases) assert.equal(speedPoints(s), p, `${s}s`);
});

test('case without a function call: first sound only', () => {
  assert.deepEqual(caseSpeed({ firstSound: 2.2 }), { first: 80, after: null, speed: 80, reason: null });
});

test('case with a function call: average of first sound and answer after the result', () => {
  // filler at 1.2 s (100) but the answer comes 4.8 s after the result (70 on the after-lookup table) -> 85
  assert.equal(caseSpeed({ firstSound: 1.2, afterTool: 4.8, toolCalled: true }).speed, 85);
  // the after-lookup table is more lenient: 3.5 s after the result is still 100 (first-sound table: 60)
  assert.equal(caseSpeed({ firstSound: 1.2, afterTool: 3.5, toolCalled: true }).after, 100);
  // nothing said after the result -> that half is 0
  const r = caseSpeed({ firstSound: 1.2, afterTool: null, toolCalled: true });
  assert.equal(r.speed, 50);
  assert.equal(r.reason, '조회 뒤 답 없음');
});

test('interruption, no sound or failure -> 0', () => {
  assert.equal(caseSpeed({ firstSound: 1.0, zeroReason: '끼어듦 의심' }).speed, 0);
  assert.equal(caseSpeed({ firstSound: null }).speed, 0);
});

test('system score = 50% response + 20% function + 30% speed, then / 5', () => {
  const s = systemScore({ response: 1, func: 1, speed: 80 });
  assert.equal(s.total, 94);
  assert.equal(s.item20, 18.8);
  assert.equal(systemScore({ response: 0.7, func: 0, speed: 50 }).total, 50);
});

test('unjudged case gives only the speed part', () => {
  assert.deepEqual(systemScore({ response: null, func: 1, speed: 60 }), { total: null, item20: null, speedPart: 18 });
});

test('average: speed over all cases, total over judged cases', () => {
  const a = averageScores([{ response: 1, func: 1, speed: 100 }, { response: 0, func: 0, speed: 0 },
    { response: null, func: null, speed: 50 }]);
  assert.equal(a.n, 3);
  assert.equal(a.judged, 2);
  assert.equal(a.speed, 50);
  assert.equal(a.total, 50);
  assert.equal(a.item20, 10);
});

test('select values parse to judgments', () => {
  assert.equal(parseJudgment(''), null);
  assert.equal(parseJudgment('0.7'), 0.7);
  assert.equal(parseJudgment('0'), 0);
});
