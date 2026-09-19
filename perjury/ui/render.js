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

/** Authoritative internal stages (unchanged). Used to map onto the judge-facing phases below. */
const STAGES = [
  ['baseline', 'Baseline'], ['context', 'Context'], ['planning', 'Plan'], ['mutation_execution', 'Fan-out'],
  ['survivor_analysis', 'Survivor'], ['test_generation', 'Test'], ['verification', 'Proof'],
  ['rescoring', 'Re-score'],
];
const STAGE_LABEL = Object.fromEntries(STAGES);

/** Judge-facing story: each phase groups one or more internal stages. Presentation only.
 * `exec` marks phases backed by real execution (green when done); the rest are model proposals. */
const PHASES = [
  { key: 'baseline', name: 'Baseline', stages: ['baseline'], exec: true },
  { key: 'attack', name: 'Attack', stages: ['context', 'planning'], exec: false },
  { key: 'execute', name: 'Execute', stages: ['mutation_execution'], exec: true },
  { key: 'investigate', name: 'Investigate', stages: ['survivor_analysis'], exec: false },
  { key: 'harden', name: 'Harden', stages: ['test_generation'], exec: false },
  { key: 'prove', name: 'Prove', stages: ['verification'], exec: true },
  { key: 'measure', name: 'Measure', stages: ['rescoring'], exec: true },
];
const TERMINAL = ['verified', 'rejected', 'inconclusive', 'failed'];

/** Falsy children (null/false from conditional sections) are dropped, never stringified.
 * @param {Element} el @param {(Node|null|false|undefined)[]} kids */
function fill(el, kids) { el.replaceChildren(...kids.filter((k) => !!k)); }
/** @param {string} id */
function body(id) { return /** @type {Element} */ (document.querySelector(`#${id} .body`)); }
const tag = (/** @type {string} */ kind, /** @type {string} */ text) => h('span', { class: `tag ${kind}` }, [text]);
const secs = (/** @type {number} */ ms) => `${(ms / 1000).toFixed(1)}s`;
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---- animation memory: cosmetic only. State is always rendered directly; classes just add polish.
const mem = { run: '', first: true, status: /** @type {Map<string,string>} */ (new Map()), seen: /** @type {Set<string>} */ (new Set()) };
/** One-time reveal class; never on the first paint of a run (restore/reload stays still). @param {string} key */
function reveal(key) {
  if (mem.seen.has(key)) return '';
  mem.seen.add(key);
  return mem.first ? '' : ' is-new';
}

/** Copy existing text to the clipboard (presentation convenience; no data leaves the page).
 * @param {string} text @param {string} label */
function copyBtn(text, label) {
  const b = h('button', { type: 'button', class: 'copy', 'aria-label': `Copy ${label}` }, ['Copy']);
  b.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; }
    catch { b.textContent = 'Copy failed'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1400);
  });
  return b;
}

/** Empty/waiting/not-reached state. @param {boolean} running @param {string} before @param {string} during */
function waiting(running, before, during) {
  if (current && TERMINAL.includes(current.stage)) {
    return [h('p', { class: 'idle' }, [`Not reached — the run ended ${current.stage}` +
      `${current.reason ? ` (${current.reason})` : ''} before this step.`])];
  }
  if (!running) return [h('p', { class: 'idle' }, [before])];
  return [h('p', { class: 'idle' }, [during]), h('div', { class: 'skel', 'aria-hidden': 'true' }, [h('i'), h('i'), h('i')])];
}

/** @param {string} line */
function hunkStart(line) {
  const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
  return m ? [Number(m[1]), Number(m[2])] : [1, 1];
}

/** Unified diff as a code-review block (file header, old/new line gutters, +/- counts). Rendering only.
 * @param {string} diff @param {string} path */
export function diffView(diff, path) {
  const lines = diff.split('\n').filter((l, i, a) => !(i === a.length - 1 && l === ''));
  const firstHunk = lines.findIndex((l) => l.startsWith('@@'));
  // only the ---/+++ file headers before the first hunk are dropped; a removed line whose own text
  // starts with "-- " must still be shown
  const shown = lines.filter((l, i) => !((firstHunk < 0 || i < firstHunk) && (l.startsWith('--- ') || l.startsWith('+++ '))));
  const adds = shown.filter((l) => l.startsWith('+')).length;
  const dels = shown.filter((l) => l.startsWith('-')).length;
  const pre = h('pre', { class: 'diff', tabindex: '0', 'aria-label': `Diff of ${path}: ${adds} added, ${dels} removed` });
  let [oldN, newN] = [1, 1];
  for (const line of shown) {
    if (line.startsWith('@@')) {
      [oldN, newN] = hunkStart(line);
      pre.append(h('span', { class: 'hunk' }, [h('span', { class: 'ln' }, ['']), h('span', { class: 'ln' }, ['']),
        h('span', { class: 'g' }, ['']), h('span', { class: 'tx' }, [line + '\n'])]));
      continue;
    }
    const c = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '';
    pre.append(h('span', c ? { class: c } : null, [
      h('span', { class: 'ln', 'aria-hidden': 'true' }, [c === 'add' ? '' : String(oldN++)]),
      h('span', { class: 'ln', 'aria-hidden': 'true' }, [c === 'del' ? '' : String(newN++)]),
      h('span', { class: 'g', 'aria-hidden': 'true' }, [c === 'add' ? '+' : c === 'del' ? '−' : '']),
      h('span', { class: 'tx' }, [line.slice(1) + '\n']),
    ]));
  }
  return h('div', { class: 'review' }, [
    h('div', { class: 'fh' }, [h('span', { class: 'path' }, [path]),
      h('span', { class: 'stat' }, [h('span', { class: 'a' }, [`+${adds}`]), ' ', h('span', { class: 'd' }, [`−${dels}`])])]),
    pre,
  ]);
}

/** Semantic outcomes arrive UPPERCASE from the API and are shown verbatim (e.g. TEST_FAIL). @param {string} v */
function outcomeBadge(v) { return h('span', { class: `badge ${v.toLowerCase()}` }, [v]); }
/** Mutation statuses arrive lowercase. @param {string} v */
function statusBadge(v) { return h('span', { class: `badge ${v}` }, [v.replace('_', ' ')]); }

/** @type {RunSnapshot|null} */
let current = null;
let announced = '';
let focusRun = '';
/** @type {string|null} */
let focusPanel = null;

/** During a live run, bring the newest surface into view. Restored runs never jump. @param {RunSnapshot|null} s */
function followFocus(s) {
  const next = !s ? null : s.score_before ? 'p-score' : s.verification ? 'p-proof' : s.proposal ? 'p-test'
    : s.survivor_id ? 'p-survivor' : null;
  if (!s || s.run_id !== focusRun) { focusRun = s?.run_id ?? ''; focusPanel = next; return; }
  if (next && next !== focusPanel) {
    focusPanel = next;
    document.getElementById(next)?.scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
  }
}

/** @param {RunSnapshot|null} s */
function phaseIndex(s) {
  if (!s) return -1;
  const stage = lastStage(s);
  const idx = PHASES.findIndex((p) => p.stages.includes(stage));
  return idx >= 0 ? idx : 0;
}

/** @param {RunSnapshot|null} s */
function pipelineStates(s) {
  if (!s) return PHASES.map(() => 'todo');
  const idx = phaseIndex(s);
  const terminal = TERMINAL.includes(s.stage);
  return PHASES.map((_, i) => {
    if (i < idx) return 'done';
    if (i > idx) return 'todo';
    if (!terminal) return 'active';
    return s.stage === 'verified' ? 'done' : 'stopped';
  });
}

/** @param {import('./contract.js').Score} sc @param {string} label @param {string} cls @param {string} key */
function scoreCell(sc, label, cls, key) {
  const undefinedScore = sc.score == null;
  const animate = !reduced() && reveal(`bar:${key}`) !== '';
  const width = undefinedScore ? 0 : Math.max(0, Math.min(1, Number(sc.score))) * 100; // backend score, not recomputed
  const bar = h('i', { style: `width:${animate ? 0 : width}%` });
  if (animate) requestAnimationFrame(() => requestAnimationFrame(() => { bar.style.width = `${width}%`; }));
  return h('div', { class: `metric ${cls}` }, [
    h('span', { class: 'm-l' }, [label]),
    h('strong', { class: undefinedScore ? 'na' : cls }, [pct(sc.score)]),
    undefinedScore ? null : h('div', { class: `bar ${cls}`, 'aria-hidden': 'true' }, [bar]),
    h('span', { class: 'm-c' }, [`${sc.killed} killed · ${sc.survived} survived`]),
    undefinedScore && h('span', { class: 'warn' }, ['no valid killed/survived outcome']),
  ]);
}

/** Header pills. @param {RunSnapshot|null} s */
function renderStatus(s) {
  const simulated = !document.getElementById('sim-banner')?.hidden;
  const mode = document.getElementById('mode-pill');
  if (mode) {
    mode.textContent = simulated ? 'SIMULATED' : 'REAL API';
    mode.className = `pill mode ${simulated ? 'sim' : 'live'}`;
    mode.title = simulated ? 'Scripted mock replay — not evidence' : 'Real run API and SSE stream';
  }
  const state = document.getElementById('state-pill');
  if (!state) return;
  let text = 'Ready'; let kind = 'idle-state';
  if (s) {
    if (s.stage === 'verified') { text = 'Verified'; kind = 'ok'; }
    else if (s.stage === 'rejected') { text = 'Rejected'; kind = 'bad'; }
    else if (s.stage === 'inconclusive') { text = 'Inconclusive'; kind = 'warn'; }
    else if (s.stage === 'failed') { text = 'Failed'; kind = 'bad'; }
    else { text = `Running · ${PHASES[phaseIndex(s)].name}`; kind = 'run'; }
  }
  state.textContent = text;
  state.className = `pill state ${kind}`;
}

/** Single #cta lives in the idle hero, and in the top bar once a run exists. @param {boolean} idle */
function placeCta(idle) {
  const cta = document.getElementById('cta');
  const slot = document.getElementById(idle ? 'cta-slot-hero' : 'cta-slot-header');
  if (!cta || !slot || cta.parentElement === slot) return;
  const hadFocus = document.activeElement === cta;
  slot.append(cta);
  if (hadFocus) cta.focus();
}

/** Workspace details that only come from authoritative run state (never invented). @param {RunSnapshot|null} s */
function renderWorkspace(s) {
  const dl = document.getElementById('ws-live');
  if (!dl) return;
  const target = s?.mutations?.[0]?.file_path;
  const rows = /** @type {[string,string][]} */ ([]);
  if (target) rows.push(['target', target]);
  if (s?.mutations?.length) rows.push(['mutations', `${s.mutations.length} semantic`]);
  rows.push(['execution', 'Modal isolated']);
  if (s?.commit_sha) rows.push(['commit', s.commit_sha]);
  dl.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', null, [k]), h('dd', { title: v }, [v])]));
}

/** Compact run summary from existing state only. @param {RunSnapshot|null} s */
function renderSummary(s) {
  const el = document.getElementById('summary-cells');
  if (!el) return;
  const muts = s?.mutations ?? [];
  const resolved = muts.filter((m) => m.status !== 'pending').length;
  const survived = muts.filter((m) => m.status === 'survived').length;
  const cell = (/** @type {string} */ k, /** @type {Node|string} */ v, /** @type {string} */ cls = '') =>
    h('div', { class: `cell ${cls}` }, [h('span', { class: 'cell-h' }, [k]), h('div', { class: 'cv' }, [v])]);
  const verdict = s?.result?.verdict ?? s?.verification?.verdict;
  el.replaceChildren(
    cell('Mutations', muts.length ? `${resolved} / ${muts.length} executed` : '—'),
    cell('Survivors', muts.length && resolved === muts.length ? String(survived) : muts.length ? `${survived} so far` : '—', survived ? 'gap' : ''),
    cell('Selected', s?.survivor_id ?? '—'),
    cell('Verdict', verdict ? verdict.toUpperCase() : '—', verdict ? `v-${verdict}` : ''),
  );
}

/** Collapsed technical inspector: identifiers and timings already present in state. @param {RunSnapshot|null} s */
function renderInspector(s) {
  const el = document.getElementById('inspector-body');
  if (!el) return;
  if (!s) { el.replaceChildren(h('p', { class: 'idle' }, ['Run details appear here after a run starts.'])); return; }
  const rows = /** @type {[string,string][]} */ ([['run id', s.run_id]]);
  if (s.commit_sha) rows.push(['commit', s.commit_sha]);
  if (s.batch_sha256) rows.push(['batch identity', s.batch_sha256]);
  if (s.context_sha256) rows.push(['context hash', s.context_sha256]);
  if (s.survivor_id) rows.push(['selected mutation', s.survivor_id]);
  if (s.candidate) rows.push(['candidate file', s.candidate.candidate_path], ['candidate sha256', s.candidate.sha256]);
  if (s.reason) rows.push(['terminal reason', s.reason]);
  const stages = s.stages ?? [];
  el.replaceChildren(
    h('dl', { class: 'insp-dl' }, rows.flatMap(([k, v]) => [h('dt', null, [k]), h('dd', null, [h('code', null, [v]), copyBtn(v, k)])])),
    stages.length ? h('div', { class: 'timings' }, [h('span', { class: 'cell-h' }, ['Stage timings']),
      h('ol', null, stages.map((st) => h('li', null, [h('span', null, [STAGE_LABEL[st.stage] ?? st.stage]), h('code', null, [secs(st.duration_ms)])])))]) : null,
    h('p', { class: 'insp-note' }, [tag('fact', 'FACT'), ' executed by pytest in isolated Sandboxes · ', tag('model', 'MODEL'),
      ' Gemini proposals, untrusted until executed · ', tag('derived', 'DERIVED'), ' computed by the backend from execution evidence. The browser never recomputes a score.']),
  );
}

/** @param {RunSnapshot|null} s @param {(id:string)=>void} select @param {string|null} selected */
export function render(s, select, selected) {
  current = s;
  if ((s?.run_id ?? '') !== mem.run) { mem.run = s?.run_id ?? ''; mem.first = true; mem.status.clear(); mem.seen.clear(); }
  const idle = !s;
  document.body.classList.toggle('is-idle', idle);
  for (const a of document.querySelectorAll('.nav a')) {
    const key = a.getAttribute('data-nav');
    if (key !== 'overview') { if (idle) a.setAttribute('aria-disabled', 'true'); else a.removeAttribute('aria-disabled'); }
    a.setAttribute('href', key === 'overview' ? (idle ? '#overview' : '#main') : key === 'mutations' ? '#p-fanout' : '#p-proof');
  }
  placeCta(idle);
  renderStatus(s);
  renderWorkspace(s);
  renderSummary(s);
  renderInspector(s);
  const running = !!s && !TERMINAL.includes(s.stage);

  // progress rail
  const states = pipelineStates(s);
  fill(/** @type {Element} */ (document.getElementById('pipeline')), PHASES.map((p, i) =>
    h('li', {
      class: `${states[i]}${states[i] === 'done' ? (p.exec ? ' exec' : ' model') : ''}`, 'data-stage': p.key,
      ...(states[i] === 'active' ? { 'aria-current': 'step' } : {}),
      title: `${p.exec ? 'Execution-backed' : 'Model proposal'} · internal: ${p.stages.map((k) => STAGE_LABEL[k]).join(' + ')}`,
    }, [h('span', { class: 'dot', 'aria-hidden': 'true' }), h('span', { class: 'nm' }, [p.name]), h('span', { class: 'sr-only' }, [` — ${states[i]}`])])));
  const live = document.getElementById('live-stage');
  const now = s ? (TERMINAL.includes(s.stage) ? `Run ${s.stage}` : `Phase: ${PHASES[phaseIndex(s)].name}`) : '';
  if (live && now !== announced) { live.textContent = now; announced = now; }

  // baseline (summary cell)
  fill(body('p-baseline'), !s?.baseline
    ? [h('div', { class: 'cv idle' }, [running ? 'running…' : '—'])]
    : [h('div', { class: 'cv' }, [outcomeBadge(s.baseline.outcome), h('span', { class: 'bs' }, [`${s.baseline.summary || ''} · ${secs(s.baseline.duration_ms)}`])])]);

  // mutation matrix
  const muts = s?.mutations ?? [];
  const inspected = selected ?? s?.survivor_id ?? null;
  fill(body('p-fanout'), !muts.length
    ? waiting(running, 'Gemini proposes 6–10 semantic mutations.', 'Planning and validating mutations…')
    : [
      h('div', { class: 'matrix' }, muts.map((m) => {
        const picked = m.id === s?.survivor_id;
        const prev = mem.status.get(m.id);
        mem.status.set(m.id, m.status);
        const changed = !mem.first && prev !== undefined && prev !== m.status;
        const b = h('button', {
          type: 'button', class: `mut ${m.status}${picked ? ' picked' : ''}${m.id === selected ? ' sel' : ''}${changed ? ' is-new' : ''}`,
          title: `${m.description}\n${m.hypothesis}`, 'data-mutation': m.id, 'data-status': m.status,
          'aria-label': `${m.id}, ${m.status.replace('_', ' ')}${picked ? ', selected survivor' : ''}: ${m.description}. Inspect.`,
          'aria-pressed': String(m.id === inspected),
        }, [
          h('span', { class: 'r1' }, [h('b', { class: 'id' }, [m.id]), statusBadge(m.status),
            m.duration_ms != null ? h('span', { class: 'dur' }, [secs(m.duration_ms)]) : null]),
          h('span', { class: 'desc' }, [m.description]),
          h('span', { class: 'r3' }, [h('span', { class: 'mini fact' }, ['FACT']),
            picked ? h('span', { class: 'pick' }, ['Selected survivor']) : m.id === inspected ? h('span', { class: 'insp' }, ['Inspecting']) : null]),
        ]);
        b.addEventListener('click', () => {
          select(m.id);
          document.getElementById('p-survivor')?.scrollIntoView({ block: 'start', behavior: reduced() ? 'auto' : 'smooth' });
        });
        return b;
      })),
    ]);

  // investigation: master (diff) / detail (analysis + execution)
  const shown = muts.find((m) => m.id === inspected);
  const analysis = s?.analysis;
  const isGap = shown?.status === 'survived';
  fill(body('p-survivor'), !shown
    ? waiting(running && muts.length > 0, 'The mutant your tests failed to catch appears here.', 'Waiting for surviving mutants…')
    : [
      h('p', { class: `lede${isGap ? ' gap' : ''}${reveal(`lede:${shown.id}`)}` }, [isGap ? 'Behaviour changed. Existing tests did not notice.' : shown.status === 'killed' ? 'Behaviour changed. Existing tests caught it.' : `Mutant ${shown.status.replace('_', ' ')}.`]),
      h('div', { class: 'md' }, [
        h('div', { class: 'md-main' }, [diffView(shown.diff, shown.file_path)]),
        h('aside', { class: 'md-side' }, [
          h('div', { class: 'kv-block' }, [h('span', { class: 'cell-h' }, ['Mutation']),
            h('p', { class: 'kv-id' }, [h('b', null, [shown.id]), statusBadge(shown.status), shown.id === s?.survivor_id ? h('span', { class: 'chip' }, ['Selected']) : null]),
            h('p', null, [shown.description])]),
          analysis && analysis.mutation_id === shown.id ? h('div', { class: `kv-block model${reveal(`an:${shown.id}`)}` }, [
            h('span', { class: 'cell-h' }, ['Gemini analysis ', tag('model', 'MODEL')]),
            h('p', null, [h('b', null, ['Potential test gap. ']), analysis.behavioural_gap]),
            h('p', { class: 'muted' }, [analysis.reasoning]),
            analysis.possibly_equivalent && h('p', { class: 'warn' }, ['Model flags this mutant as possibly equivalent.']),
          ]) : null,
          shown.hypothesis && shown.hypothesis !== shown.description
            ? h('div', { class: 'kv-block' }, [h('span', { class: 'cell-h' }, ['Hypothesis ', tag('model', 'MODEL')]), h('p', { class: 'muted' }, [shown.hypothesis])]) : null,
          h('div', { class: 'kv-block' }, [h('span', { class: 'cell-h' }, ['Execution ', tag('fact', 'FACT')]),
            h('dl', { class: 'mini-dl' }, [h('dt', null, ['result']), h('dd', null, [shown.status.replace('_', ' ')]),
              shown.duration_ms != null ? h('dt', null, ['duration']) : null, shown.duration_ms != null ? h('dd', null, [secs(shown.duration_ms)]) : null,
              h('dt', null, ['file']), h('dd', null, [shown.file_path])])]),
        ]),
      ]),
    ]);

  // generated defence
  const p = s?.proposal;
  const path = s?.candidate?.candidate_path ?? p?.target_file ?? '';
  const verdictNow = s?.result?.verdict ?? s?.verification?.verdict;
  fill(body('p-test'), !p
    ? waiting(running && !!s?.survivor_id, 'Gemini writes one focused pytest test for the selected gap.', 'Generating a candidate regression test…')
    : [
      h('div', { class: `review artefact${reveal('test')}` }, [
        h('div', { class: 'fh' }, [h('span', { class: 'path' }, [path]),
          h('span', { class: 'fh-tags' }, [s?.candidate ? h('span', { class: 'newfile' }, ['NEW FILE']) : null, tag('model', 'MODEL'), copyBtn(p.test_code, 'test code')])]),
        h('pre', { class: 'code', tabindex: '0', 'aria-label': 'Generated test code' }, [p.test_code]),
      ]),
      h('p', { class: 'trust' }, verdictNow === 'verified'
        ? [h('span', { class: 'chip ok' }, ['Verified by execution']), ' Trusted only after the two-world proof below.']
        : [h('span', { class: 'chip model' }, ['Unverified proposal']), ' PERJURY has not trusted this test yet.']),
      s?.candidate ? h('p', { class: 'note' }, [tag('fact', 'FACT'), ' Created as a separate isolated file ', h('code', { 'data-candidate': '1' }, [s.candidate.candidate_path]),
        ` — ${p.target_file} is untouched.`]) : null,
    ]);

  // two-world proof: the climax
  const v = s?.verification;
  const runVerdict = s?.result?.verdict;
  const verdict = runVerdict ?? v?.verdict;
  document.getElementById('p-proof')?.classList.toggle('is-verified', verdict === 'verified');
  fill(body('p-proof'), !v
    ? waiting(running && !!p, 'Original must PASS; the mutant must TEST_FAIL.', 'Executing the original and mutant worlds…')
    : [
      h('div', { class: 'proof' }, [
        h('div', { class: `world w-orig${reveal('world:orig')}` }, [h('span', { class: 'w-k' }, ['Original']), h('span', { class: 'w-s' }, ['Candidate test']),
          outcomeBadge(v.original), h('span', { class: 'w-t' }, [`required PASS · ${secs(v.original_duration_ms)}`])]),
        h('span', { class: 'vs', 'aria-hidden': 'true' }, ['vs']),
        h('div', { class: `world w-mut${reveal('world:mut')}` }, [h('span', { class: 'w-k' }, ['Mutant']), h('span', { class: 'w-s' }, ['Same candidate']),
          outcomeBadge(v.mutant), h('span', { class: 'w-t' }, [`required TEST_FAIL · ${secs(v.mutant_duration_ms)}`])]),
      ]),
      verdict ? h('div', { class: `verdict ${verdict}${reveal(`verdict:${verdict}`)}`, 'data-verdict': verdict }, [h('span', { class: 'vd' }, [verdict.toUpperCase()]),
        verdict === 'verified' ? h('span', { class: 'vt' }, ['Execution proved the candidate distinguishes the two behaviours.']) : null])
        : h('p', { class: 'idle' }, ['Awaiting deterministic verdict…']),
      runVerdict && v.verdict !== runVerdict ? h('p', { class: 'note' }, [`Candidate verdict: ${v.verdict}. Run ended ${runVerdict}: ${s?.result?.reason ?? ''}`]) : null,
      verdict !== 'verified' ? h('p', { class: 'note' }, [s?.result ? s.result.explanation : v.explanation]) : null,
    ]);

  // measurement — every number is copied from the backend (score_*, comparison); no arithmetic
  const b0 = s?.score_before; const a = s?.score_after; const c = s?.comparison;
  const dl = deltaLabel(c);
  const gained = c?.newly_killed_ids?.length ?? 0;
  fill(body('p-score'), !b0
    ? waiting(running && !!v, 'After VERIFIED, the same batch is re-run with the new test.', 'Re-running the same mutation batch…')
    : [
      h('div', { class: 'score' }, [
        scoreCell(b0, 'Before', '', 'before'),
        h('div', { class: 'delta' }, [h('b', { class: `dl ${dl.tone}${reveal('delta')}`, 'data-delta': dl.text }, [dl.text]), h('span', null, ['same mutation batch'])]),
        a ? scoreCell(a, 'After', 'good', 'after') : h('div', { class: 'metric' }, [h('span', { class: 'm-l' }, ['After']), h('span', { class: 'idle' }, ['…'])]),
      ]),
      gained > 0 && c?.status !== 'inconsistent'
        ? h('p', { class: 'plain' }, [`${gained} additional behavioural change${gained > 1 ? 's are' : ' is'} now detected (${c.newly_killed_ids.join(', ')}).`]) : null,
      c?.status === 'inconsistent' ? h('p', { class: 'alert-inline', 'data-status': 'inconsistent' }, [`Re-score inconsistent — no improvement claimed. ${c.message}`]) : null,
      h('p', { class: 'note' }, [`Excluded from the score (invalid/timeout/infra): ${b0.excluded} before` + (a ? ` · ${a.excluded} after` : '') + '.']),
    ]);

  const meta = s ? [`run ${s.run_id}`, s.commit_sha ? `commit ${s.commit_sha}` : '',
    s.batch_sha256 ? `batch ${s.batch_sha256.slice(7, 15)}` : ''].filter(Boolean).join(' · ') : '';
  document.getElementById('run-meta')?.replaceChildren(meta);
  followFocus(s);
  if (s) mem.first = false;
}

/** @param {number} ms */
export function renderClock(ms) {
  const c = document.getElementById('clock'); if (c) c.textContent = clock(ms);
  const bar = /** @type {HTMLElement|null} */ (document.getElementById('budget-bar'));
  if (bar) { bar.style.width = `${Math.min(100, (ms / 120000) * 100)}%`; bar.classList.toggle('over', ms > 120000); }
}
