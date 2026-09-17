# Contributing

Install the source tree with `python -m pip install -e .`, then run `python -m unittest discover -s tests -v`. The runtime should remain usable without external Python packages. Keep database schema changes explicit and add migration tests when changing durable fields.

For a bug report, include Python and SQLite versions, a minimal synthetic reproduction, the expected result and the actual result. Avoid including private conversation databases. For retrieval changes, preserve a fixed pre-change baseline, record parameters before measurement and distinguish development data from evaluation data.

Do not present unit-test success as agent reliability or retrieval recall as generated-answer accuracy. Update metric definitions, denominators, raw rows and claim tables together. Keep benchmark answers and evidence labels out of index creation, query expansion and prompt packing.
