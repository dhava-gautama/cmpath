# Contributing

Repository: <https://github.com/dhava-gautama/cmpath>. Open an issue before a
large protocol or schema change so compatibility and evidence requirements can
be agreed first.

Install the source tree with `python -m pip install -e .`, then run `python -m unittest discover -s tests -v`. The runtime should remain usable without external Python packages. Keep database schema changes explicit and add migration tests when changing durable fields.

Checkout line endings are part of the schema contract. The native engine embeds `native/engine/base.sql` and `journal.sql`, and SQLite stores a `CREATE` statement exactly as it was given, so the root `.gitattributes` pins `*.sql` to LF while leaving it text. Do not mark a `.sql` file binary and do not commit one with CRLF endings: the release verifier compares source and distribution payloads byte for byte, and an embedded schema that varies by platform would make those comparisons depend on the build host.

For a bug report, include Python and SQLite versions, a minimal synthetic reproduction, the expected result and the actual result. Avoid including private conversation databases. For retrieval changes, preserve a fixed pre-change baseline, record parameters before measurement and distinguish development data from evaluation data.

Do not present unit-test success as agent reliability or retrieval recall as generated-answer accuracy. Update metric definitions, denominators, raw rows and claim tables together. Keep benchmark answers and evidence labels out of index creation, query expansion and prompt packing.

Never commit conversation databases, live provider captures, credentials,
compiled native binaries, or local build outputs. The root `.gitignore` covers
the standard locations; review staged files before every public push.
