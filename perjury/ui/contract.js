// @ts-check
// UI contract for the run API (#17), reconciled with docs/API_CONTRACT.md (authoritative).
// Outcomes are UPPERCASE (baseline.outcome, verification.original/mutant); mutation statuses are
// lowercase. Scores and the before/after comparison come from the backend and are never recomputed.

/** @typedef {'killed'|'survived'|'invalid'|'timeout'|'infra_error'|'pending'|'running'} MutationStatus */
/** @typedef {'created'|'baseline'|'context'|'planning'|'mutation_execution'|'survivor_analysis'|'test_generation'|'verification'|'rescoring'|'verified'|'rejected'|'inconclusive'|'failed'} RunStage */

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
 * @property {string=} manifest_sha256
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
 * @property {'verified'|'rejected'|'inconclusive'} verdict   deterministic candidate verdict
 * @property {string} explanation
 */
/**
 * @typedef {Object} Score
 * @property {number} killed
 * @property {number} survived
 * @property {number} excluded   invalid + timeout + infra_error (outside the denominator)
 * @property {number|null} score authoritative 0..1, or null when there is no valid killed/survived
 *   outcome. NEVER render null as 0%; the UI never recomputes it.
 * @property {'scored'|'inconclusive'=} state
 */
/**
 * @typedef {Object} Comparison  authoritative before/after comparison (#25)
 * @property {'confirmed'|'inconsistent'} status
 * @property {number|null} delta          after.score - before.score, null when unavailable
 * @property {'improved'|'unchanged'|'regressed'|'unavailable'} direction
 * @property {string} batch_sha256
 * @property {string[]} newly_killed_ids
 * @property {string[]} newly_survived_ids
 * @property {string} message
 */
/**
 * @typedef {Object} Candidate  the generated test as materialized (separate isolated file)
 * @property {string} mutation_id
 * @property {string} candidate_path
 * @property {string} sha256
 * @property {string} diff
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
 * @property {{verdict:'verified'|'rejected'|'inconclusive', explanation:string, reason?:string}|null} result
 * @property {Score|null} score_before
 * @property {Score|null} score_after
 * @property {Comparison|null} comparison
 * @property {Candidate|null} candidate
 * @property {{code:string, message:string}|null} error
 * @property {string=} reason
 * @property {string=} context_sha256
 * @property {string=} batch_sha256
 * @property {boolean=} terminal
 * @property {{stage:string, status:string, duration_ms:number}[]=} stages   (server snapshot only)
 * @property {string=} progress_stage   last working stage seen (client-side, from events)
 */
/**
 * @typedef {Object} RunEvent
 * @property {string} run_id
 * @property {number} seq
 * @property {string} type   run.started | baseline.completed | context.completed | plan.completed |
 *   mutation.completed | execution.completed | survivor.selected | analysis.completed |
 *   test.proposed | candidate.ready | verification.completed | rescore.mutation.completed |
 *   rescore.completed | run.completed | run.failed   (mutation.started is NOT emitted)
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
 * @property {(runId: string, onEvent: (e: RunEvent) => void, onClose: (err?: Error) => void, afterSeq?: number) => () => void} subscribe
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
    comparison: null,
    candidate: null,
    error: null,
  };
}
