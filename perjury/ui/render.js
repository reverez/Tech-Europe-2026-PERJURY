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
const tag = (/** @type {string} */ kind, /** @type {string} */ text) => h('span', { class: `tag ${kind}` }, [text]);
const secs = (/** @type {number} */ ms) => `${(ms / 1000).toFixed(1)}s`;

/** Empty state before a run; a skeleton + wait message while this run has not reached the panel;
 * an explicit "not reached" note when a finished run stopped before this panel.
 * @param {boolean} running @param {string} before @param {string} during */
function waiting(running, before, during) {
  if (current && TERMINAL.includes(current.stage)) {
    return [h('p', { class: 'idle' }, [`Not reached — the run ended ${current.stage.toUpperCase()}` +
      `${current.reason ? ` (${current.reason})` : ''} before this step. ${before}`])];
  }
  if (!running) return [h('p', { class: 'idle' }, [before])];
  return [h('p', { class: 'idle' }, [during]), h('div', { class: 'skel', 'aria-hidden': 'true' }, [h('i'), h('i'), h('i')])];
}

/** Unified diff as a code-review block (file header + gutter). Rendering only.
 * @param {string} diff @param {string} path */
export function diffView(diff, path) {
  const lines = diff.split('\n').filter((l, i, a) => !(i === a.length - 1 && l === ''));
  const firstHunk = lines.findIndex((l) => l.startsWith('@@'));
  // only the ---/+++ file headers before the first hunk are dropped; a removed line whose own text
  // starts with "-- " must still be shown
  const shown = lines.filter((l, i) => !((firstHunk < 0 || i < firstHunk) && (l.startsWith('--- ') || l.startsWith('+++ '))));
  const adds = shown.filter((l) => l.startsWith('+')).length;
  const dels = shown.filter((l) => l.startsWith('-')).length;
  const pre = h('pre', { class: 'diff', tabindex: '0', 'aria-label': `Diff of ${path}` });
  for (const line of shown) {
    const c = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
    const g = c === 'add' ? '+' : c === 'del' ? '−' : '';
    const text = c === 'add' || c === 'del' ? line.slice(1) : c === 'hunk' ? line : line.slice(1);
    pre.append(h('span', c ? { class: c } : null, [h('span', { class: 'g', 'aria-hidden': 'true' }, [g]), text + '\n']));
  }
  return h('div', { class: 'review' }, [
    h('div', { class: 'fh' }, [h('span', null, [path]),
      h('span', { class: 'stat' }, [h('span', { class: 'a' }, [`+${adds}`]), ' ', h('span', { class: 'd' }, [`−${dels}`])])]),
    pre,
  ]);
}

/** Semantic outcomes arrive UPPERCASE from the API and are shown verbatim (e.g. TEST_FAIL).
 * @param {string} v */
function outcomeBadge(v) {
  return h('span', { class: `badge ${v.toLowerCase()}` }, [v]);
}
/** Mutation statuses arrive lowercase. @param {string} v */
function statusBadge(v) {
  return h('span', { class: `badge ${v}` }, [v.replace('_', ' ')]);
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
    h('small', null, [label.toUpperCase()]),
    h('strong', { class: undefinedScore ? 'na' : cls }, [pct(sc.score)]),
    // bar width is the backend score itself; nothing is recomputed here
    undefinedScore ? null : h('div', { class: `bar ${cls}`, 'aria-hidden': 'true' }, [h('i', { style: `width:${Math.max(0, Math.min(1, Number(sc.score))) * 100}%` })]),
    h('small', null, [`${sc.killed} killed · ${sc.survived} survived`]),
    undefinedScore && h('small', { class: 'warn' }, ['no valid killed/survived outcome']),
  ]);
}

/** @type {RunSnapshot|null} */
let current = null;
let announced = '';
let focusRun = '';
/** @type {string|null} */
let focusPanel = null;

/** During a live run, bring the newest panel into view (presentation only). A restored run
 * (first render already complete) does not jump; reduced-motion scrolls instantly.
 * @param {RunSnapshot|null} s */
function followFocus(s) {
  const next = !s ? null : s.score_before ? 'p-score' : s.verification ? 'p-proof' : s.proposal ? 'p-test'
    : s.survivor_id ? 'p-survivor' : null;
  if (!s || s.run_id !== focusRun) { focusRun = s?.run_id ?? ''; focusPanel = next; return; }
  if (next && next !== focusPanel) {
    focusPanel = next;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.getElementById(next)?.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
  }
}

/** @param {RunSnapshot|null} s @param {(id:string)=>void} select @param {string|null} selected */
export function render(s, select, selected) {
  current = s;
  const running = !!s && !TERMINAL.includes(s.stage);
  const states = pipelineStates(s);
  fill(/** @type {Element} */ (document.getElementById('pipeline')), STAGES.map(([key, label], i) =>
    h('li', { class: states[i], 'data-stage': key, ...(states[i] === 'active' ? { 'aria-current': 'step' } : {}) }, [
      h('span', { class: 'dot', 'aria-hidden': 'true' }, [states[i] === 'done' ? '✓' : states[i] === 'stopped' ? '!' : String(i + 1)]),
      h('span', null, [label]),
      h('span', { class: 'sr-only' }, [` — ${states[i]}`]),
    ])));
  const live = document.getElementById('live-stage');
  const now = s ? (TERMINAL.includes(s.stage) ? `Run ${s.stage}` : `Stage: ${s.stage.replace('_', ' ')}`) : '';
  if (live && now !== announced) { live.textContent = now; announced = now; }

  // 1 baseline
  fill(body('p-baseline'), !s?.baseline
    ? waiting(running, 'Start a run: PERJURY first proves the existing suite is green.', 'Running the existing test suite…')
    : [
      h('div', { class: 'big-status' }, [outcomeBadge(s.baseline.outcome), h('span', null, ['existing suite'])]),
      h('dl', { class: 'kv' }, [
        h('dt', null, ['result']), h('dd', null, [s.baseline.summary || '—']),
        h('dt', null, ['time']), h('dd', null, [secs(s.baseline.duration_ms)]),
      ]),
      h('p', { class: 'note' }, ['Green tests prove only what they cover.']),
    ]);

  // 2 fan-out (cards go pending -> terminal status; there is no "running" event)
  const muts = s?.mutations ?? [];
  fill(body('p-fanout'), !muts.length
    ? waiting(running, 'Gemini proposes 6–10 semantic mutations; each runs against the real suite in its own isolated Sandbox.', 'Planning and validating mutations…')
    : [
      h('div', { class: 'mhead' }, [
        h('span', null, [`${muts.length} validated mutations · click a card to inspect its diff`]),
        h('span', null, [statusBadge('killed'), ' test caught it']),
        h('span', null, [statusBadge('survived'), ' potential test gap']),
      ]),
      h('div', { class: 'mgrid' }, muts.map((m) => {
        const picked = m.id === s?.survivor_id;
        const b = h('button', {
          type: 'button', class: `mut ${m.status}${picked ? ' picked' : ''}${m.id === selected ? ' sel' : ''}`,
          title: `${m.description}\n${m.hypothesis}`, 'data-mutation': m.id, 'data-status': m.status,
          'aria-label': `${m.id}, ${m.status.replace('_', ' ')}${picked ? ', selected survivor' : ''}: ${m.description}`,
          'aria-pressed': String(m.id === (selected ?? s?.survivor_id)),
        }, [
          picked ? h('span', { class: 'pick', 'aria-hidden': 'true' }, ['SELECTED']) : null,
          h('b', null, [m.id]), h('span', { class: 'st' }, [m.status.replace('_', ' ')]), h('small', null, [m.description]),
        ]);
        b.addEventListener('click', () => select(m.id));
        return b;
      })),
    ]);

  // 3 survivor
  const shown = muts.find((m) => m.id === (selected ?? s?.survivor_id));
  const analysis = s?.analysis;
  fill(body('p-survivor'), !shown
    ? waiting(running && muts.length > 0, 'The mutant your tests failed to catch appears here, with its exact diff and Gemini’s analysis.', 'Waiting for surviving mutants…')
    : [
      h('div', { class: 'headline' }, [h('b', null, [shown.id]), h('span', null, [shown.description]), statusBadge(shown.status),
        shown.id === s?.survivor_id ? h('span', { class: 'tag fact' }, ['SELECTED SURVIVOR']) : null]),
      diffView(shown.diff, shown.file_path),
      analysis && analysis.mutation_id === shown.id ? h('div', { class: 'model-box' }, [
        h('p', null, [tag('model', 'MODEL'), ' ', h('b', null, ['Potential test gap: ']), analysis.behavioural_gap]),
        h('p', null, [h('b', null, ['Reasoning: ']), analysis.reasoning]),
        analysis.possibly_equivalent && h('p', { class: 'warn' }, ['Model flags this mutant as possibly equivalent.']),
      ]) : null,
    ]);

  // 4 generated test (model proposal) + how it was materialized (executed fact)
  const p = s?.proposal;
  const path = s?.candidate?.candidate_path ?? p?.target_file ?? '';
  fill(body('p-test'), !p
    ? waiting(running && !!s?.survivor_id, 'Gemini writes one focused pytest test aimed at the selected survivor.', 'Generating a candidate regression test…')
    : [
      h('p', { class: 'note', style: 'margin-top:0' }, ['Model proposal — not proof until it is executed in both worlds.']),
      h('div', { class: 'review' }, [
        h('div', { class: 'fh' }, [h('span', null, [path]), s?.candidate ? h('span', { class: 'newfile' }, ['NEW FILE']) : null]),
        h('pre', { class: 'code', tabindex: '0', 'aria-label': 'Generated test code' }, [p.test_code]),
      ]),
      s?.candidate ? h('p', { class: 'note' }, [
        tag('fact', 'FACT'), ' materialized as a separate isolated file ', h('code', { 'data-candidate': '1' }, [s.candidate.candidate_path]),
        ` — ${p.target_file} is untouched.`,
      ]) : null,
    ]);

  // 5 proof — the climax
  const v = s?.verification;
  const runVerdict = s?.result?.verdict;
  const verdict = runVerdict ?? v?.verdict;
  fill(body('p-proof'), !v
    ? waiting(running && !!p, 'The candidate runs twice: on the original code (must PASS) and on the mutant (must TEST_FAIL).', 'Executing original and mutant worlds…')
    : [
      h('div', { class: 'proof' }, [
        h('div', { class: 'world' }, [h('span', { class: 'lbl' }, ['ORIGINAL + CANDIDATE']), h('span', { class: 'req' }, ['required: PASS']),
          outcomeBadge(v.original), h('small', null, [secs(v.original_duration_ms)])]),
        h('div', { class: 'world' }, [h('span', { class: 'lbl' }, ['MUTANT + CANDIDATE']), h('span', { class: 'req' }, ['required: TEST_FAIL']),
          outcomeBadge(v.mutant), h('small', null, [secs(v.mutant_duration_ms)])]),
      ]),
      verdict ? h('div', { class: `verdict ${verdict}`, 'data-verdict': verdict }, [verdict.toUpperCase()])
        : h('p', { class: 'idle' }, ['Awaiting deterministic verdict…']),
      verdict === 'verified' ? h('p', { class: 'proved' }, ['Execution proved the regression test distinguishes the two behaviors.']) : null,
      runVerdict && v.verdict !== runVerdict
        ? h('p', { class: 'note' }, [`Candidate verdict: ${v.verdict}. Run ended ${runVerdict}: ${s?.result?.reason ?? ''}`]) : null,
      verdict !== 'verified' ? h('p', { class: 'note' }, [s?.result ? s.result.explanation : v.explanation]) : null,
    ]);

  // 6 score — every number here is copied from the backend (score_*, comparison); no arithmetic
  const b0 = s?.score_before; const a = s?.score_after; const c = s?.comparison;
  const dl = deltaLabel(c);
  fill(body('p-score'), !b0
    ? waiting(running && !!v, 'After VERIFIED, the exact same mutation batch is re-run with the new test.', 'Re-scoring the same batch with the verified test…')
    : [
      h('div', { class: 'score' }, [
        scoreCell(b0, 'before', ''),
        h('div', { class: 'delta' }, [h('span', { class: 'arrow', 'aria-hidden': 'true' }, ['→']), h('b', { class: `dl ${dl.tone}`, 'data-delta': dl.text }, [dl.text])]),
        a ? scoreCell(a, 'after', 'good') : h('div', null, [h('small', null, ['AFTER']), h('span', { class: 'idle' }, ['…'])]),
      ]),
      c?.status === 'inconsistent'
        ? h('p', { class: 'alert-inline', 'data-status': 'inconsistent' }, [`Re-score inconsistent — no improvement claimed. ${c.message}`])
        : null,
      h('p', { class: 'note' }, [
        'Same validated batch, re-executed with the verified test. Excluded (invalid/timeout/infra): ' +
        `${b0.excluded} before` + (a ? ` · ${a.excluded} after` : '') + '.',
      ]),
    ]);

  const meta = s ? [`run ${s.run_id}`, s.commit_sha ? `commit ${s.commit_sha}` : '',
    s.batch_sha256 ? `batch ${s.batch_sha256.slice(7, 15)}` : ''].filter(Boolean).join(' · ') : '';
  document.getElementById('run-meta')?.replaceChildren(meta);
  followFocus(s);
}

/** @param {number} ms */
export function renderClock(ms) {
  const c = document.getElementById('clock'); if (c) c.textContent = clock(ms);
  const bar = /** @type {HTMLElement|null} */ (document.getElementById('budget-bar'));
  if (bar) { bar.style.width = `${Math.min(100, (ms / 120000) * 100)}%`; bar.classList.toggle('over', ms > 120000); }
}
