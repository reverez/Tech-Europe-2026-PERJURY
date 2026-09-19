// @ts-check
import { replay } from './store.js';
import { mockEvents } from './mock_events.js';

/** @typedef {import('./contract.js').RunAdapter} RunAdapter */

/** Real transport: same-origin POST/GET/SSE from #17 (the only path used in live mode). @returns {RunAdapter} */
export function liveAdapter() {
  /** @param {Response} r */
  async function json(r) {
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const b = await r.json();
        msg = b?.detail?.message ?? b?.detail ?? b?.message ?? msg;
      } catch { /* non-JSON error body */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return r.json();
  }
  return {
    kind: 'live',
    start: async () => (await json(await fetch('/api/runs', { method: 'POST' }))).run_id,
    snapshot: async (id) => json(await fetch(`/api/runs/${encodeURIComponent(id)}`)),
    subscribe(id, onEvent, onClose, afterSeq = 0) {
      const cursor = afterSeq > 0 ? `?after=${afterSeq}` : '';
      const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/events${cursor}`);
      es.onmessage = (m) => {
        try { onEvent(JSON.parse(m.data)); } catch (e) { onClose(/** @type {Error} */ (e)); }
      };
      es.onerror = () => { es.close(); onClose(new Error('event stream closed')); };
      return () => es.close();
    },
  };
}

/**
 * MOCK transport for UI development and demo-shell rehearsal only.
 * Selected explicitly with ?adapter=mock; the UI shows a permanent SIMULATED banner.
 * It replays scripted events and is never a substitute for a real run.
 * @param {{speed?: number}} [opts] @returns {RunAdapter}
 */
export function mockAdapter({ speed = 1 } = {}) {
  const runId = 'mock-run-0001';
  const events = mockEvents(runId);
  let delivered = 0;
  return {
    kind: 'mock',
    start: async () => runId,
    snapshot: async (id) => ({ ...replay(id, events.slice(0, delivered).map((x) => x.event)) }),
    subscribe(_id, onEvent, onClose, afterSeq = 0) {
      let cancelled = false;
      (async () => {
        for (const { delay, event } of events.filter((x) => x.event.seq > afterSeq)) {
          await new Promise((r) => setTimeout(r, delay / speed));
          if (cancelled) return;
          delivered += 1;
          onEvent(event);
        }
        onClose();
      })();
      return () => { cancelled = true; };
    },
  };
}
