# Evaluation protocol

This protocol was written before the first version 0.3 public-benchmark scores were computed. It is a local analysis plan, not an externally registered preregistration. Runtime parameters are fixed before the first run. Any corrections or later exploratory changes are disclosed in the report.

## Public retrieval evaluation

Use all 500 instances of the cleaned LongMemEval-S release, dataset revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`, and all 10 conversations in the released `locomo10.json`, repository revision `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`. Keep source hashes in the results. Fetch external datasets separately; do not bundle their conversations with the product distribution.

The retrieval unit is one original message for LongMemEval and one speaker turn for LoCoMo. Each input session becomes an explicit storage task. These supplied boundaries test storage and retrieval, not automatic task detection. Index only role/content and session/date provenance. Do not expose question answers, `has_answer`, evidence annotations, generated summaries or observations to the memory index or ranker.

Compare three untrained methods over the same original message atoms: recency, unique query-token overlap with recency tie breaking, and the product's SQLite FTS5 BM25. All lexical methods use the same fixed Unicode query tokenizer and English stop-word list. FTS5 uses its built-in k1=1.2 and b=0.75. No aliases or structured facts are supplied on public data. No query-specific threshold tuning is allowed.

For LongMemEval, exclude the 30 `_abs` questions from retrieval recall because they have no target event. Report message recall when `has_answer` identifies at least one message, and session recall against `answer_session_ids`. For LoCoMo, report categories 1-4 with nonempty, resolvable evidence IDs; report all exclusion counts, including category 5 and invalid IDs. Speaker text is indexed without inferred image contents. This study does not calculate official answer accuracy.

Report mean fraction of gold messages retrieved at k=8 and k=20, all-gold-message success at k=8, and fraction of gold sessions represented at k=8 messages. Also pack the first 32 ranked candidates into the same complete-message JSON evidence envelope under 2,000 estimated input units. Admit whole messages, skip oversized atoms, preserve the full query, and calculate gold-message recall and all-gold coverage in the packed context. Do not read gold labels during packing. A prompt export has no answer labels in its transmitted messages.

For comparative uncertainty, bootstrap paired differences in message recall@8 with 2,000 draws and seed 20260910. Resample LoCoMo at the conversation level, because questions share a history. Resample LongMemEval at the question level and disclose that overlapping filler histories weaken independence assumptions. Intervals describe these released benchmark samples, not generalization to unseen real users. Retain raw query-level rows and per-category results. Record indexing, lexical retrieval and packing costs separately; do not call them model latency.

## Operational validation

Test transactions, reopened databases, standalone backups, search-index deletion, identifier stability, optimistic state updates, source-linked revisions, task lineage and exact counted payloads. Randomized interleaving tests compare the implementation with an independently maintained key/value reference state. They are software validation, not a benchmark of natural-language reasoning.

A separate task-state study uses 40 seeded sessions with 12 independent tasks, overlapping fact keys and revised values. Probe explicit IDs, registered aliases, unregistered paraphrases, ambiguous aliases and absent task IDs. Report resolution coverage, conditional correctness and false resolutions separately. Do not turn abstention into success on answerable unregistered paraphrases. Test that selecting evidence never changes the active task. Compare scoped and global context for inclusion of facts from unrelated tasks; explain that scope is supplied by the caller.

Measure indexed search at 1,000, 10,000 and 50,000 messages, with 100 fixed-form queries per size, a fixed synthetic generator and separate ingestion timing. Report database size including the WAL after writes. The microbenchmark is local and its corpora are artificial. Do not claim asymptotic constant latency, web-service throughput or multi-tenant readiness.

## Claims outside this study

No language model or learned embedding model is available in the configured environment. Model answer quality, semantic paraphrase robustness, autonomous task-boundary detection and full agent task completion are not measured. Neither LongMemEval-V2 nor commercial memory-provider leaderboard results are claimed. The release may be positioned as an inspectable task-memory component with a lexical retrieval baseline.
