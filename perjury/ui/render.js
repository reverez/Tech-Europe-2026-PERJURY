// @ts-check
import { clock, deltaLabel, lastStage, pct } from './store.js';

/** @typedef {import('./contract.js').RunSnapshot} RunSnapshot */

/**
 * Tiny safe DOM builder: all model/executed text goes through text nodes, never parsed as markup.
 * @param {string} tag @param {Record<string,string>|null} [attrs] @param {(Node|string|null|undefined|false)[]} [kids]
 */
export function h(tag, attrs = null, kids = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) el.setAttribute(k, v);
  for (const k of kids) if (k) el.append(k);
  return el;
}

const STAGES = [
  ['baseline', 'Baseline'], ['context', 'Context'], ['planning', 'Plan'], ['mutation_execution', 'Fan-out'],
  ['survivor_analysis', 'Survivor'], ['test_generation', 'Test'], ['verification', 'Proof'],
  ['rescoring', 'Re-score'],
];
const KEYS = STAGES.map((s) => s[0]);
const TERMINAL = ['verified', 'rejected', 'inconclusive', 'failed'];

/** Falsy children (null/false from conditional sections) are dropped, never stringified.
 * @param {Element} el @param {(Node|null|false|undefined)[]} kids */
function fill(el, kids) { el.replaceChildren(...kids.filter((k) => !!k)); }
/** @param {string} id */
function body(id) { return /** @type {Element} */ (document.querySelector(`#${id} .body`)); }
const idle = (/** @type {string} */ t) => h('p', { class: 'idle' }, [t]);
const tag = (/** @type {string} */ kind, /** @type {string} */ text) => h('span', { class: `tag ${kind}` }, [text]);
const secs = (/** @type {number} */ ms) => `${(ms / 1000).toFixed(1)}s`;

/** @param {string} diff */
export function diffView(diff) {
  const pre = h('pre', { class: 'diff' });
  for (const line of diff.split('\n')) {
    const c = line.startsWith('+') && !line.startsWith('+++') ? 'add'
      : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
    pre.append(h('span', c ? { class: c } : null, [line + '\n']));
  }
  return pre;
}

/** Semantic outcomes arrive UPPERCASE from the API; classes are lowercase.
 * @param {string} v */
function outcomeBadge(v) {
  return h('span', { class: `badge ${v.toLowerCase()}` }, [v.replace('_', ' ')]);
}

/** @param {RunSnapshot|null} s */
function pipelineStates(s) {
  if (!s) return KEYS.map(() => 'todo');
  const idx = KEYS.indexOf(lastStage(s));
  const terminal = TERMINAL.includes(s.stage);
  return KEYS.map((_, i) => {
    if (i < idx) return 'done';
    if (i > idx) return 'todo';
    if (!terminal) return 'active';
    return s.stage === 'verified' ? 'done' : 'stopped';
  });
}

/** @param {import('./contract.js').Score} sc @param {string} label @param {string} cls */
function scoreCell(sc, label, cls) {
  const undefinedScore = sc.score == null;
  return h('div', null, [
    h('small', null, [label]),
    h('strong', { class: undefinedScore ? 'na' : cls }, [pct(sc.score)]),
    h('small', null, [`${sc.killed} killed · ${sc.survived} survived`]),
    undefinedScore && h('small', { class: 'warn' }, ['no valid killed/survived outcome']),
  ]);
}

/** @param {RunSnapshot|null} s @param {(id:string)=>void} select @param {string|null} selected */
export function render(s, select, selected) {
  const states = pipelineStates(s);
  fill(/** @type {Element} */ (document.getElementById('pipeline')), STAGES.map(([key, label], i) =>
    h('li', { class: states[i], 'data-stage': key }, [label])));

  // 1 baseline
  fill(body('p-baseline'), !s?.baseline ? [idle('Waiting for a run.')] : [
    outcomeBadge(s.baseline.outcome),
    h('p', null, [`${s.baseline.summary ?? ''} · ${secs(s.baseline.duration_ms)}`]),
    h('p', { class: 'note' }, ['Green tests prove only what they cover.']),
  ]);

  // 2 fan-out (cards go pending -> terminal status; there is no "running" event)
  const muts = s?.mutations ?? [];
  fill(body('p-fanout'), !muts.length ? [idle('Mutations appear once Gemini has proposed them.')] : [
    h('div', { class: 'mgrid' }, muts.map((m) => {
      const b = h('button', {
        type: 'button', class: `mut ${m.status}${m.id === s?.survivor_id ? ' picked' : ''}${m.id === selected ? ' sel' : ''}`,
        title: `${m.description}\n${m.hypothesis}`, 'data-mutation': m.id, 'data-status': m.status,
      }, [h('b', null, [m.id]), h('span', null, [m.status.replace('_', ' ')]), h('small', null, [m.description])]);
      b.addEventListener('click', () => select(m.id));
      return b;
    })),
  ]);

  // 3 survivor
  const shown = muts.find((m) => m.id === (selected ?? s?.survivor_id));
  const analysis = s?.analysis;
  fill(body('p-survivor'), !shown ? [idle('The surviving mutant, its diff and explanation appear here.')] : [
    h('p', null, [h('b', null, [shown.id]), ' — ', shown.description, ' ', outcomeBadge(shown.status)]),
    diffView(shown.diff),
    analysis && analysis.mutation_id === shown.id ? h('div', { class: 'model-box' }, [
      h('p', null, [h('b', null, ['Potential test gap: ']), analysis.behavioural_gap]),
      h('p', null, [h('b', null, ['Reasoning: ']), analysis.reasoning]),
      analysis.possibly_equivalent && h('p', { class: 'warn' }, ['Model flags this mutant as possibly equivalent.']),
    ]) : null,
  ]);

  // 4 generated test (model proposal) + how it was materialized (executed fact)
  const p = s?.proposal;
  fill(body('p-test'), !p ? [idle('Candidate test appears after survivor analysis.')] : [
    h('p', { class: 'note' }, ['Model proposal — not proof until executed.']),
    h('pre', { class: 'code' }, [p.test_code]),
    s?.candidate ? h('p', { class: 'note' }, [
      tag('fact', 'FACT'), ' created as a separate isolated file: ', h('code', { 'data-candidate': '1' }, [s.candidate.candidate_path]),
      ` (existing ${p.target_file} untouched)`,
    ]) : h('p', { class: 'note' }, [`Target context: ${p.target_file}`]),
  ]);

  // 5 proof
  const v = s?.verification;
  const runVerdict = s?.result?.verdict;
  const verdict = runVerdict ?? v?.verdict;
  fill(body('p-proof'), !v ? [idle('Original and mutant executions appear here.')] : [
    h('div', { class: 'proof' }, [
      h('div', null, ['original + candidate', outcomeBadge(v.original), h('small', null, [secs(v.original_duration_ms)])]),
      h('div', null, ['mutant + candidate', outcomeBadge(v.mutant), h('small', null, [secs(v.mutant_duration_ms)])]),
    ]),
    verdict ? h('div', { class: `verdict ${verdict}`, 'data-verdict': verdict }, [verdict.toUpperCase()])
      : h('p', { class: 'idle' }, ['Awaiting deterministic verdict…']),
    runVerdict && v.verdict !== runVerdict
      ? h('p', { class: 'note' }, [`Candidate verdict: ${v.verdict}. Run ended ${runVerdict}: ${s?.result?.reason ?? ''}`]) : null,
    s?.result ? h('p', { class: 'note' }, [s.result.explanation]) : h('p', { class: 'note' }, [v.explanation]),
  ]);

  // 6 score — every number here is copied from the backend (score_*, comparison); no arithmetic
  const b0 = s?.score_before; const a = s?.score_after; const c = s?.comparison;
  const dl = deltaLabel(c);
  fill(body('p-score'), !b0 ? [idle('Same-batch before/after score appears after re-scoring.')] : [
    h('div', { class: 'score' }, [
      scoreCell(b0, 'before', ''),
      h('div', { class: 'delta' }, [h('span', { class: 'arrow' }, ['→']), h('b', { class: `dl ${dl.tone}`, 'data-delta': dl.text }, [dl.text])]),
      a ? scoreCell(a, 'after', 'good') : h('div', null, [h('small', null, ['after']), h('strong', null, ['…'])]),
    ]),
    c?.status === 'inconsistent'
      ? h('p', { class: 'alert-inline', 'data-status': 'inconsistent' }, [`Re-score inconsistent — no improvement claimed. ${c.message}`])
      : null,
    h('p', { class: 'note' }, [
      `Excluded from score (invalid/timeout/infra): ${b0.excluded} before` + (a ? ` · ${a.excluded} after` : '') +
      '. Same mutation batch re-run with the verified test.',
    ]),
  ]);

  const meta = s ? [`run ${s.run_id}`, s.commit_sha ? `commit ${s.commit_sha}` : '',
    s.batch_sha256 ? `batch ${s.batch_sha256.slice(7, 15)}` : ''].filter(Boolean).join(' · ') : '';
  document.getElementById('run-meta')?.replaceChildren(meta);
}

/** @param {number} ms */
export function renderClock(ms) {
  const c = document.getElementById('clock'); if (c) c.textContent = clock(ms);
  const bar = /** @type {HTMLElement|null} */ (document.getElementById('budget-bar'));
  if (bar) { bar.style.width = `${Math.min(100, (ms / 120000) * 100)}%`; bar.classList.toggle('over', ms > 120000); }
}
