// @ts-check
import { clock, pct } from './store.js';

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
  ['baseline', 'Baseline'], ['planning', 'Plan'], ['mutation_execution', 'Fan-out'],
  ['survivor_analysis', 'Survivor'], ['test_generation', 'Test'], ['verification', 'Proof'],
  ['rescoring', 'Re-score'],
];
const ORDER = ['created', ...STAGES.map((s) => s[0]), 'verified'];

/** @param {Element} el @param {Node[]} kids */
function fill(el, kids) { el.replaceChildren(...kids); }
/** @param {string} id */
function body(id) { return /** @type {Element} */ (document.querySelector(`#${id} .body`)); }
const idle = (/** @type {string} */ t) => h('p', { class: 'idle' }, [t]);

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

/** @param {string} v */
function outcomeBadge(v) {
  return h('span', { class: `badge ${v.toLowerCase()}` }, [v.replace('_', ' ')]);
}

/** @param {RunSnapshot|null} s @param {(id:string)=>void} select @param {string|null} selected */
export function render(s, select, selected) {
  const stageIdx = s ? ORDER.indexOf(s.stage === 'rejected' || s.stage === 'inconclusive' ? 'verified' : s.stage) : -1;
  fill(/** @type {Element} */ (document.getElementById('pipeline')), STAGES.map(([key, label], i) => {
    const state = !s ? 'todo' : s.stage === 'failed' ? (i < stageIdx ? 'done' : 'todo')
      : i + 1 < stageIdx ? 'done' : i + 1 === stageIdx ? 'active' : stageIdx === ORDER.length - 1 ? 'done' : 'todo';
    return h('li', { class: state, 'data-stage': key }, [label]);
  }));

  // 1 baseline
  fill(body('p-baseline'), !s?.baseline ? [idle('Waiting for a run.')] : [
    outcomeBadge(s.baseline.outcome),
    h('p', null, [`${s.baseline.summary ?? ''} · ${(s.baseline.duration_ms / 1000).toFixed(1)}s`]),
    h('p', { class: 'note' }, ['Green tests prove only what they cover.']),
  ]);

  // 2 fan-out
  const muts = s?.mutations ?? [];
  fill(body('p-fanout'), !muts.length ? [idle('Mutations appear once Gemini has proposed them.')] : [
    h('div', { class: 'mgrid' }, muts.map((m) => {
      const b = h('button', {
        type: 'button', class: `mut ${m.status}${m.id === s?.survivor_id ? ' picked' : ''}${m.id === selected ? ' sel' : ''}`,
        title: `${m.description}\n${m.hypothesis}`,
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

  // 4 test
  const p = s?.proposal;
  fill(body('p-test'), !p ? [idle('Candidate test appears after survivor analysis.')] : [
    h('p', { class: 'note' }, ['Model proposal — not proof until executed.']),
    h('p', null, [h('code', null, [p.target_file])]),
    h('pre', { class: 'code' }, [p.test_code]),
  ]);

  // 5 proof
  const v = s?.verification;
  const verdict = s?.result?.verdict;
  fill(body('p-proof'), !v ? [idle('Original and mutant executions appear here.')] : [
    h('div', { class: 'proof' }, [
      h('div', null, ['original + candidate', outcomeBadge(v.original), h('small', null, [`${(v.original_duration_ms / 1000).toFixed(1)}s`])]),
      h('div', null, ['mutant + candidate', outcomeBadge(v.mutant), h('small', null, [`${(v.mutant_duration_ms / 1000).toFixed(1)}s`])]),
    ]),
    verdict ? h('div', { class: `verdict ${verdict}` }, [verdict.toUpperCase()]) : h('p', { class: 'idle' }, ['Awaiting deterministic verdict…']),
    s?.result ? h('p', { class: 'note' }, [s.result.explanation]) : null,
  ]);

  // 6 score — values come straight from the authoritative snapshot
  const a = s?.score_after; const b0 = s?.score_before;
  fill(body('p-score'), !b0 ? [idle('Same-batch before/after score appears after re-scoring.')] : [
    h('div', { class: 'score' }, [
      h('div', null, [h('small', null, ['before']), h('strong', null, [pct(b0.score)]), h('small', null, [`${b0.killed} killed · ${b0.survived} survived`])]),
      h('span', { class: 'arrow' }, ['→']),
      h('div', null, [h('small', null, ['after']), h('strong', { class: 'good' }, [a ? pct(a.score) : '…']), h('small', null, [a ? `${a.killed} killed · ${a.survived} survived` : ''])]),
    ]),
    h('p', { class: 'note' }, [`Excluded from score: ${b0.excluded} invalid/timeout/infra. Same mutation batch re-run with the verified test.`]),
  ]);

  document.getElementById('run-meta')?.replaceChildren(
    s ? `run ${s.run_id}${s.commit_sha ? ` · commit ${s.commit_sha}` : ''}` : '');
}

/** @param {number} ms */
export function renderClock(ms) {
  const c = document.getElementById('clock'); if (c) c.textContent = clock(ms);
  const bar = /** @type {HTMLElement|null} */ (document.getElementById('budget-bar'));
  if (bar) { bar.style.width = `${Math.min(100, (ms / 120000) * 100)}%`; bar.classList.toggle('over', ms > 120000); }
}
