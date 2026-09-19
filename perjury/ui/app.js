// @ts-check
import { liveAdapter, mockAdapter } from './adapters.js';
import { isTerminal } from './contract.js';
import { reduce } from './store.js';
import { render, renderClock } from './render.js';

const params = new URLSearchParams(location.search);
const adapter = params.get('adapter') === 'mock'
  ? mockAdapter({ speed: Number(params.get('speed') ?? 1) || 1 })
  : liveAdapter();

/** @type {import('./contract.js').RunSnapshot|null} */
let snap = null;
/** @type {string|null} */
let selected = null;
let unsub = () => {};
let timer = 0;
let t0 = 0;

const cta = /** @type {HTMLButtonElement} */ (document.getElementById('cta'));
const alertBox = /** @type {HTMLElement} */ (document.getElementById('alert'));
document.getElementById('sim-banner')?.toggleAttribute('hidden', adapter.kind !== 'mock');

function paint() { render(snap, (id) => { selected = id; paint(); }, selected); }
function fail(/** @type {string} */ msg) {
  alertBox.textContent = msg; alertBox.hidden = false;
  cta.disabled = false; cta.textContent = 'Retry run'; clearInterval(timer);
}

/** Terminal state is always reconciled with the authoritative snapshot. */
async function finish() {
  clearInterval(timer);
  try {
    if (snap) snap = await adapter.snapshot(snap.run_id);
    renderClock(snap?.elapsed_ms ?? Date.now() - t0);
  } catch (e) { fail(`Could not fetch final snapshot: ${/** @type {Error} */ (e).message}`); return; }
  if (snap?.stage === 'failed') fail(snap.error?.message ?? 'Run failed.');
  else { cta.disabled = false; cta.textContent = 'Run again'; }
  paint();
}

const MAX_RESUBSCRIBES = 5;

/** Follow the SSE stream; if it drops before a terminal event, recover from the authoritative
 * snapshot and resume after the last seen seq (bounded retries).
 * @param {string} runId @param {number} startedAt @param {number} [attempt] */
function follow(runId, startedAt, attempt = 0) {
  t0 = startedAt;
  clearInterval(timer);
  timer = setInterval(() => renderClock(Date.now() - t0), 250);
  unsub = adapter.subscribe(runId, (ev) => {
    if (!snap) return;
    snap = reduce(snap, ev);
    if (ev.type === 'plan.completed') selected = null;
    paint();
    if (isTerminal(snap.stage)) { unsub(); finish(); }
  }, async (err) => {
    if (!snap || isTerminal(snap.stage)) return;
    try {
      snap = await adapter.snapshot(runId); paint();
      if (isTerminal(snap.stage)) return finish();
    } catch { /* fall through to retry */ }
    if (attempt >= MAX_RESUBSCRIBES) return fail(`Lost event stream${err ? `: ${err.message}` : ''}`);
    setTimeout(() => follow(runId, t0, attempt + 1), 500 * (attempt + 1));
  }, snap?.last_seq ?? 0);
}

cta.addEventListener('click', async () => {
  unsub(); alertBox.hidden = true; selected = null; snap = null; paint(); renderClock(0);
  cta.disabled = true; cta.textContent = 'Running…';
  try {
    const runId = await adapter.start();
    history.replaceState(null, '', `#run=${encodeURIComponent(runId)}`);
    snap = await adapter.snapshot(runId);
    paint();
    follow(runId, snap.started_at_ms ?? Date.now());
  } catch (e) { fail(`Could not start run: ${/** @type {Error} */ (e).message}`); }
});

// Refresh support: #run=<id> renders the snapshot; terminal runs stay terminal, live ones resume.
const m = /^#run=(.+)$/.exec(location.hash);
if (m && adapter.kind === 'live') {
  const id = decodeURIComponent(m[1]);
  adapter.snapshot(id).then((s) => {
    snap = s; paint();
    if (s.elapsed_ms != null) renderClock(s.elapsed_ms);
    if (isTerminal(s.stage)) { cta.textContent = 'Run again'; if (s.stage === 'failed') fail(s.error?.message ?? 'Run failed.'); }
    else { cta.disabled = true; cta.textContent = 'Running…'; follow(id, s.started_at_ms ?? Date.now()); }
  }).catch((e) => fail(`Could not load run ${id}: ${e.message}`));
} else paint();

// ?autostart=1 clicks the CTA on load (rehearsal / screenshots).
if (params.get('autostart') === '1' && !m) cta.click();
