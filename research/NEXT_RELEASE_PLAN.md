# CMP next-release plan: provenance and effect eligibility

Status: design decision after the 0.4.0a3 freshness audit (2026-09-12).

## Decision

The 0.4.0a3 scope certificate is a publishable, bounded result—not the final
memory product. The next release should add two separable controls:

1. **Explicit provenance for derived state.** A reply that writes a snapshot or
   fact declares the evidence IDs it relied on. CMP stores those edges in the
   journal and checks that every edge still exists, is not retracted, and still
   belongs to an admissible scope before allowing a new action or commit.
2. **Action eligibility immediately before effects.** A host asks CMP to
   revalidate the turn and obtain a short-lived local eligibility record before
   invoking a tool. Commit remains required afterwards. The API must label this
   as a TOCTOU boundary: CMP cannot atomically control an arbitrary remote
   service unless that service participates in the transaction.

These controls should be implemented independently and benchmarked separately.
The first closes the currently demonstrated stale-derived-snapshot failure. The
second reduces the post-admission window and makes any residual effect race
observable instead of silently treating a stale commit rejection as success.

## Proposed wire shape

```json
{
  "text": "…",
  "snapshot": {"next": "verify"},
  "provenance": [
    {"evidence_id": 481, "kind": "supports"},
    {"evidence_id": 492, "kind": "contradicts"}
  ]
}
```

`supports` edges are required for a derived write; `contradicts` edges are
retained for audit and make the write ineligible unless the contradiction is
resolved explicitly. Evidence references are immutable IDs, not copied text.
Missing, deleted, or retracted references fail closed with `stale_context`.

For action eligibility, the host supplies the intended call ID after recording
the tool intent; CMP derives the hash from the exact recorded name and
arguments. It returns an eligibility token containing the turn
generation, current scope certificate, and argument hash. `FinishTool` rejects
tokens from another generation, with a changed argument hash, or past expiry;
`Commit` still performs its own scope/revision fence. A connector
that needs atomicity must consume this token in its own transaction; the core
cannot promise atomicity for an uncooperating HTTP/API call.

## Frozen falsification suite

Add schedules for:

- derived snapshot supported by a retracted source;
- derived snapshot supported by a deleted source;
- one valid and one invalid provenance edge;
- contradiction recorded after admission but before commit;
- argument mutation after eligibility;
- external write during the eligibility-to-effect interval;
- a participating connector that commits atomically (positive control);
- historical replay of a previously valid derived write.

Report stale actions, stale commits, unsupported writes, false blocks, and
historical replay accuracy. Keep the current a3 arms and controls unchanged so
the original 10/10 stale-intent and 3/3 positive-control claims remain directly
comparable.

## What is *not* a claim

This is not a new embedding model, a universal semantic-retrieval solution, or
a guarantee of atomicity for arbitrary remote effects. Provenance-aware shared
memory and temporal validity are active prior art: MAP-Graph treats provenance
as an operational access/action signal, while MemStrata reports deterministic
supersession for evolving facts ([MAP-Graph](https://arxiv.org/abs/2608.10509),
[MemStrata](https://arxiv.org/abs/2606.26511)). Persistent-memory write governance
is also now benchmarked directly ([PASB](https://arxiv.org/abs/2607.10526)).
CMP's defensible contribution is an executable, source-preserving protocol with
reproducible invalidation and effect-eligibility measurements across crash,
replay, scope, and provenance schedules.

## Release gate

Do not call the next version a breakthrough until the new suite demonstrates:

- zero unsupported derived writes in the supported-provenance lane;
- zero stale new intents in all pre-effect schedules;
- no silent success after an ineligible effect;
- positive-control utility retained; and
- an explicit residual TOCTOU rate for non-participating connectors.
