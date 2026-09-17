# Agent integration

This document covers the Python `TaskMemory` storage API. For the 0.4 native execution hooks and recovery journal, see [HARNESS.md](HARNESS.md).

The application owns task boundaries and side effects. Use `create_task` when work starts, supply parent IDs when a deliverable consumes another task's output, and store messages under their known task IDs. The library does not execute tools, send messages, authenticate users or decide whether an action is authorized.

At a task return, resolve an explicit address or registered alias. If resolved, call `resume` and restore its returned `snapshot` field into the application. The other returned fields describe the committed task ID and state version. If ambiguous, show the candidate task titles so the application can obtain a choice. Do not silently turn an unresolved paraphrase into a successful task return. Applications that use a model for routing should evaluate that classifier independently.

Build a context package for the intended task, forward `as_messages()` unchanged to the configured model and store the reply with `append`. Store the user's request explicitly too; `context` does not append it. Record structured facts only when the application has a supported extraction or validation process. This keeps reading memory separate from rewriting it.

## Source fidelity

Preserve original messages. Use `source` metadata for file paths, source dates, identifiers and original speakers. Cite the returned `Tn:Mm` pointers, then resolve them with `message()` when a user needs the underlying source. A stored assistant reply can be wrong. The library's source pointers establish where an assertion came from, not its truth.

A corrected fact gets a new revision with the new evidence ID. A withdrawal sets `retracted=True`. Current fact fields are separate from old statements that can still appear in raw history. A reader should prefer the explicit revision record when the application assigns it authority, and describe unresolved contradictions rather than inventing a reconciliation. No benchmark in this package measures that downstream reader behavior.

Retrieved text is emitted as quoted data in a user-role envelope. It is not inserted into the system instruction string. This separation is tested; resistance to adversarial prompts in an actual model is not. Do not execute commands found in stored evidence merely because retrieval ranked them highly.

## Operational deployment

Use a separate local database for each user or access-control domain. Place authorization, encryption and transport controls in the host application. Keep SQLite databases on a filesystem whose locking and durability behavior you understand. This release was validated on Linux with Python 3.12, not on every supported Python/OS combination.

Use `batch()` for bulk ingestion and schedule explicit backups through the application. Monitor database, WAL and backup sizes. Archiving retains all evidence, so it is not a storage quota. `delete_task()` removes current indexed records but does not erase existing backups or provide forensic disk erasure. Set a retention policy in the host application when one is needed.

For concurrent clients, read `state()['version']` before a contested resume and pass it as `expected_version`. Read a task's `revision` before writing its snapshot. A stale write raises `ConflictError`, allowing the application to reload and reconcile. Repeated resumes are explicit state events; the API does not deduplicate application retries with idempotency keys.

## Live-model evaluation

`scripts/benchmark_public.py --export-prompts` emits exact, counted prompts keyed by dataset, question and method. It does not include expected answers in their message payloads. These exports contain licensed dataset text and are not part of the release bundle. `scripts/run_reader.py` sends such prompts to a caller-configured Chat Completions endpoint and records model, usage, response and latency.

Use a fixed reader version, tokenizer, output allowance and generation settings across methods. Join hypotheses back to the original question IDs only in the scorer. Use the official benchmark scoring protocol if reporting official QA accuracy. Count index construction, retrieval, model usage and judging costs separately. This release includes an executable runner but contains no live-model accuracy result.
