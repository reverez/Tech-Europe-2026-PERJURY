// @ts-check
// Provisional UI contract for the run API (#17) and orchestrator state (#15).
// Mirrors docs/UI_CONTRACT.md. When #17 lands, reconcile field names HERE ONLY.

/** @typedef {'killed'|'survived'|'invalid'|'timeout'|'infra_error'|'pending'|'running'} MutationStatus */
/** @typedef {'created'|'baseline'|'planning'|'mutation_execution'|'survivor_analysis'|'test_generation'|'verification'|'rescoring'|'verified'|'rejected'|'inconclusive'|'failed'} RunStage */

/**
 * @typedef {Object} MutationView
 * @property {string} id
 * @property {string} file_path
 * @property {string} description       model proposal
 * @property {string} hypothesis        model proposal
 * @property {string} diff              unified diff rendered by the applicator (executed fact)
 * @property {MutationStatus} status    executed fact
 * @property {number=} duration_ms
 */
/**
 * @typedef {Object} Baseline
 * @property {'PASS'|'TEST_FAIL'|'INVALID'|'TIMEOUT'|'INFRA_ERROR'} outcome
 * @property {number} duration_ms
 * @property {string=} summary
 */
/**
 * @typedef {Object} SurvivorAnalysis
 * @property {string} mutation_id
 * @property {string} behavioural_gap
 * @property {string} test_intent
 * @property {boolean} possibly_equivalent
 * @property {string} reasoning
 */
/**
 * @typedef {Object} TestProposal
 * @property {string} mutation_id
 * @property {string} test_name
 * @property {string} target_file
 * @property {string} test_code
 * @property {string} explanation
 */
/**
 * @typedef {Object} Verification
 * @property {string} mutation_id
 * @property {'PASS'|'TEST_FAIL'|'INVALID'|'TIMEOUT'|'INFRA_ERROR'} original
 * @property {'PASS'|'TEST_FAIL'|'INVALID'|'TIMEOUT'|'INFRA_ERROR'} mutant
 * @property {number} original_duration_ms
 * @property {number} mutant_duration_ms
 */
/**
 * @typedef {Object} Score
 * @property {number} killed
 * @property {number} survived
 * @property {number} excluded   invalid + timeout + infra_error (outside the denominator)
 * @property {number} score      authoritative, 0..1; the UI never recomputes it
 */
/**
 * @typedef {Object} RunSnapshot
 * @property {string} run_id
 * @property {RunStage} stage
 * @property {number} last_seq
 * @property {number=} started_at_ms
 * @property {number=} elapsed_ms
 * @property {string=} commit_sha
 * @property {Baseline|null} baseline
 * @property {MutationView[]} mutations
 * @property {string|null} survivor_id
 * @property {SurvivorAnalysis|null} analysis
 * @property {TestProposal|null} proposal
 * @property {Verification|null} verification
 * @property {{verdict:'verified'|'rejected'|'inconclusive', explanation:string}|null} result
 * @property {Score|null} score_before
 * @property {Score|null} score_after
 * @property {{code:string, message:string}|null} error
 */
/**
 * @typedef {Object} RunEvent
 * @property {string} run_id
 * @property {number} seq
 * @property {string} type   run.started | baseline.completed | plan.completed | mutation.started |
 *   mutation.completed | survivor.selected | analysis.completed | test.proposed |
 *   verification.completed | rescore.completed | run.completed | run.failed
 * @property {RunStage=} stage
 * @property {string=} mutation_id
 * @property {any} data
 */

/**
 * Transport boundary. The UI only talks to this interface.
 * @typedef {Object} RunAdapter
 * @property {'live'|'mock'} kind
 * @property {() => Promise<string>} start                          -> run_id
 * @property {(runId: string) => Promise<RunSnapshot>} snapshot
 * @property {(runId: string, onEvent: (e: RunEvent) => void, onClose: (err?: Error) => void) => () => void} subscribe
 */

export const TERMINAL_STAGES = /** @type {const} */ (['verified', 'rejected', 'inconclusive', 'failed']);

/** @param {RunStage} stage */
export function isTerminal(stage) {
  return /** @type {readonly string[]} */ (TERMINAL_STAGES).includes(stage);
}

/** @param {string} runId @returns {RunSnapshot} */
export function emptySnapshot(runId) {
  return {
    run_id: runId,
    stage: 'created',
    last_seq: 0,
    baseline: null,
    mutations: [],
    survivor_id: null,
    analysis: null,
    proposal: null,
    verification: null,
    result: null,
    score_before: null,
    score_after: null,
    error: null,
  };
}
