# Local native benchmark

Started 2026-09-11T03:40:18Z. One serial run; p95 is nearest rank ceil(0.95*n). See research/NATIVE_PROTOCOL.md for timing boundaries and limitations.

Python 3.12.14, SQLite 3.53.1; Go go1.27.1, native SQLite 3.53.4. Platform: Linux-6.18.35-x86_64-with-glibc2.39. CPU: AMD EPYC 9V74 80-Core Processor.

| Messages | Method | Query kind | n | Median ms | p95 ms |
|---:|---|---|---:|---:|---:|
| 1000 | python_embedded | all | 100 | 0.4208 | 0.9173 |
| 1000 | python_embedded | selective | 50 | 0.0537 | 0.0922 |
| 1000 | python_embedded | broad | 50 | 0.7797 | 1.0631 |
| 1000 | go_embedded | all | 100 | 0.4773 | 0.9633 |
| 1000 | go_embedded | selective | 50 | 0.0643 | 0.1110 |
| 1000 | go_embedded | broad | 50 | 0.8551 | 0.9972 |
| 1000 | python_go_bridge | all | 100 | 0.7558 | 1.3629 |
| 1000 | python_go_bridge | selective | 50 | 0.3482 | 0.4339 |
| 1000 | python_go_bridge | broad | 50 | 1.1946 | 1.7157 |
| 10000 | python_embedded | all | 100 | 3.6339 | 7.9690 |
| 10000 | python_embedded | selective | 50 | 0.0863 | 0.1514 |
| 10000 | python_embedded | broad | 50 | 7.5615 | 8.3374 |
| 10000 | go_embedded | all | 100 | 3.8498 | 8.8211 |
| 10000 | go_embedded | selective | 50 | 0.0889 | 0.1422 |
| 10000 | go_embedded | broad | 50 | 8.0402 | 9.3653 |
| 10000 | python_go_bridge | all | 100 | 4.3851 | 9.9554 |
| 10000 | python_go_bridge | selective | 50 | 0.3947 | 0.4840 |
| 10000 | python_go_bridge | broad | 50 | 8.8220 | 10.4491 |
| 50000 | python_embedded | all | 100 | 18.8332 | 42.2771 |
| 50000 | python_embedded | selective | 50 | 0.1869 | 0.2609 |
| 50000 | python_embedded | broad | 50 | 39.4885 | 43.1426 |
| 50000 | go_embedded | all | 100 | 20.1777 | 44.1365 |
| 50000 | go_embedded | selective | 50 | 0.1943 | 0.2895 |
| 50000 | go_embedded | broad | 50 | 42.1576 | 46.4977 |
| 50000 | python_go_bridge | all | 100 | 21.2622 | 47.5770 |
| 50000 | python_go_bridge | selective | 50 | 0.6305 | 0.8742 |
| 50000 | python_go_bridge | broad | 50 | 43.9576 | 48.4569 |

| Lifecycle method | n | Median ms | p95 ms |
|---|---:|---:|---:|
| go_embedded | 100 | 1.4274 | 1.9608 |
| python_go_bridge | 100 | 2.2630 | 2.9942 |

Exact ordered-hit agreement against Python:
- 1000 messages, go_embedded: 100/100
- 1000 messages, python_go_bridge: 100/100
- 10000 messages, go_embedded: 100/100
- 10000 messages, python_go_bridge: 100/100
- 50000 messages, go_embedded: 100/100
- 50000 messages, python_go_bridge: 100/100

Actual counts: {"corpus_sizes": [1000, 10000, 50000], "search_timed": 900, "search_warmup": 90, "lifecycle_timed": 200, "lifecycle_warmup": 10, "lifecycle_begin_calls": 210, "lifecycle_commit_calls": 210, "model_calls": 0}

Environment and final native counts are retained in benchmark.json. All raw measurements are in search_rows.jsonl and lifecycle_rows.jsonl. Search warmups, process startup and corpus construction are excluded. Lifecycle includes persistence but no model or tool calls. Cache state is uncontrolled; methods use separate connection caches, rotated run order and potentially different SQLite versions. Results do not establish production performance or agent quality.
