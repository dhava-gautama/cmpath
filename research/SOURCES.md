# Source audit and data provenance

The source ledger in `sources.json` records titles, authors, dates, exact URLs and each source's role. The paper uses numbered source notes and a complete source list. Literature claims are separated from measurements made with this package. Recent 2026 preprints are treated as author reports, not independent replications.

## Scope correction

Task-addressable context is a useful engineering pattern, but graph structure, memory paging, note linking, temporal relations and source attribution all have substantial prior art. MemGPT, A-MEM, Zep, Mem0 and newer provenance-oriented work prevent a defensible claim that CMP invented structured agent memory. This release's claim is a concrete software contract with an inspectable implementation and a measured lexical baseline.

The earlier archive's claimed small-model recall curves and unspecified original CMP paper were not independently recovered. They are not evidence for this release. The source-audit work on Evonic used the pinned CMP source revision `bafbe2a6183fb672bddbc18abc21cd49e4066e47`; its map/card character bound was not a complete model-payload token guarantee. No Evonic source code is bundled or relicensed by the new runtime.

## Fixed public inputs

| Input | Revision | SHA-256 | Bytes |
| --- | --- | --- | ---: |
| LongMemEval-S cleaned | HF `98d7416c24c778c2fee6e6f3006e7a073259d48f` | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` | 277383467 |
| LoCoMo-10 | Git `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` | 2805274 |

The LongMemEval download was pinned directly. LoCoMo's initially obtained bytes were compared against the pinned revision and had the same hash. The download helper enforces both hashes. External conversations are stored separately from the distributed software and research results.

LongMemEval's official repository distinguishes retrieval evaluation from generated-answer evaluation and excludes 30 abstention questions from retrieval. This study follows that exclusion and scores 470 answerable questions. Eleven empty source messages are skipped without changing the question denominator. Their handling was an adapter correction after the first run encountered an empty message; the complete scored run was then restarted.

The released LoCoMo file has 1,986 QA entries across ten conversations. Excluding 446 category-5 questions leaves 1,540. Four have no evidence IDs and nine contain at least one unresolved ID, leaving 1,527 scored questions. Whole questions with incomplete references are excluded, rather than silently dropping missing evidence and inflating their per-question recall. These exclusions may still favor the scored subset and are reported explicitly.

## Comparability

Our LongMemEval unit is one original message, not a full session or a user-assistant round. LoCoMo uses one speaker turn. The rankers index original text only; stored speaker/date metadata does not participate in matching. Public session boundaries are supplied by the dataset. No aliases, extracted fact keys, generated observations, answer text or evidence labels are supplied to the ranker.

The paper's session recall is the proportion of annotated sessions represented in the top eight *messages*. It is not session-recall at eight retrieved sessions. The 2,000-unit envelope uses an estimated count and a common retrieval-only packer. The product's lineage/fact-aware packer is validated separately. No official QA score, semantic retrieval result or LongMemEval-V2 result is implied.

No third-party commercial performance percentage is used as a directly comparable baseline. The public-data comparison is against locally implemented recency and token-overlap methods over the same source units. The experiment shows a lexical ranker improvement; it does not isolate a graph-specific retrieval benefit.
