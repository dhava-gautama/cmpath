# Migration from research revision 0.2

Version 0.3 uses a new durable `TaskMemory` API. It is not source-compatible with `CMPSession`, does not load its SQLite eviction store directly and does not infer task boundaries from free-form turns. The earlier version remains a research baseline.

First save a **portable version-2 JSON checkpoint** from revision 0.2. That checkpoint includes the in-memory graph and all evicted rows. The raw eviction database alone does not contain the complete session.

```bash
python -m cmpath.migrate session.json migrated.db
cmpath --db migrated.db tasks
cmpath --db migrated.db check
```

Migration requires a new output path and never changes the input checkpoint. It restores the dependency graph, maps old IDs such as `A1` to explicit aliases, retains source messages and preserves planner snapshots. The command prints the ID mapping. The active task is restored when present.

Old waypoints are retained as **derived summary documents**. They are not silently converted into verified or keyed facts. Original message roles and metadata are kept as source metadata; a legacy system-role message is imported as document evidence. Empty source messages are omitted. An invalid dependency graph or conflicting message ownership fails migration before a destination database is written.

The conversion preserves state for inspection but does not recreate old automatic routing or its context-packing order. Inspect a migrated task and its source evidence before replacing a host application's old planner integration. Keep the original checkpoint as a reversible reference.
