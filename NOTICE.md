# Provenance and external materials

Versions 0.3 and 0.4 provide a new SQLite implementation of explicit task-path memory. The original runtime is in `src/cmpath`; the native engine is in `native/engine`, with a small original wrapper around SQLite in `native/internal/sqlite/sqlite.go`. It does not import or bundle Evonic's code or the earlier CMPSession implementation. The MIT license covers the new runtime, tests, examples, benchmark adapters and accompanying original documentation in this release.

The task-path idea was motivated by the supplied Context Memory Path research archive and by the public CMP module in Robin Syihab's Evonic project. Evonic is a separate AGPL-3.0 project. Citation of that project does not change its license. The earlier uploaded archive had no standalone license and is not redistributed inside this release.

External benchmark conversations are fetched separately and retain their original licenses. The cleaned LongMemEval dataset card identifies an MIT license. LoCoMo's repository provides Attribution-NonCommercial 4.0 International terms. Neither dataset is bundled in the wheel, source distribution or research package. Result files contain measurement rows and evidence identifiers, without reproducing source conversations or question texts. Prompt exports, when explicitly generated, contain source text and should remain with the separately obtained datasets.

The research paper cites external works without claiming affiliation, endorsement or independent replication of their model-quality scores. No DOI, journal acceptance, package-index registration or public repository URL is asserted.

## Native third-party components

SQLite 3.53.4 is included as upstream `sqlite3.c` and `sqlite3.h`. SQLite is public-domain software; see [SQLite copyright terms](https://sqlite.org/copyright.html). The official download, archive hashes and included-file hashes are recorded in `native/internal/sqlite/UPSTREAM.json`. The project MIT license does not relicense upstream components.

`golang.org/x/text` 0.42.0 is vendored under its BSD-style license, retained at `native/vendor/golang.org/x/text/LICENSE`. Its module checksum is in `native/go.sum`; vendored source headers and `PATENTS` notice are preserved. Source: [Go text repository](https://go.googlesource.com/text/).

The included native executables contain code from the Go runtime. The Go 1.27.1 license and patent grant are retained in `native/third_party/GO_LICENSE` and `GO_PATENTS`. The full Go compiler is not redistributed. These Linux binaries dynamically depend on the host C runtime; bundling SQLite does not create universal or fully static executables.
