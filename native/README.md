# Native Go component

The `engine` package is the embedded implementation. The `cmpath-native` command exposes the same engine through a persistent JSON-line protocol; the `cmpath-bench` command measures direct Go calls. `examples/embedded` is an executable file-checksum workflow.

## Build

From the project root:

```bash
python scripts/build_native.py
```

Requires Go 1.26+ and a GCC-compatible C compiler (MinGW-w64 or LLVM on Windows; Visual Studio `cl.exe` is not supported by Go cgo). Pass `--go /path/to/go` and `--cc /path/to/cc` when they are not on PATH. If the default Go executable is older but a compatible toolchain is already in its module cache, the script selects that cache entry directly. Use `--check-only` to validate the local tools without compiling. SQLite and the Go text dependency are vendored; the script uses `-mod=vendor`, `-buildvcs=false`, `-trimpath`, an empty build ID and stripped binaries (`-ldflags=-s -w`), with dependency/toolchain downloads disabled. It writes all commands to `native/bin`; `--output` selects another directory. C compilation can take a minute on a first build. Do not post-process a signed executable with a generic `strip` utility: rebuild with these linker flags, then sign the resulting release archive.

Validated environments include Linux x86-64/glibc 2.39 and Windows AMD64 with MSYS2 UCRT64 GCC, using bundled SQLite 3.53.4. Bundled SQLite does not imply a fully static libc or portable binaries for untested targets. On Windows, `scripts/build_native.py` emits `.exe` targets; Linux ELF binaries are not executable there, and the Python integration tests report that mismatch and skip rather than attempting to launch them. Build the Windows target with a GCC-compatible compiler, for example:

```powershell
python scripts/build_native.py --goos windows --goarch amd64 --cc C:\path\to\gcc.exe
```

The compiler must be a native Windows GCC-compatible toolchain; a WSL Linux `gcc` cannot produce a Windows cgo executable.

```bash
cd native
go test -buildvcs=false -race -count=1 ./...
go run -buildvcs=false ./examples/embedded --input README.md --request-id demo-1
```

The Go test suite needs the race detector's supported compiler/platform. The run command uses this directory's README and writes `checksum-memory.db` in the current directory.

## Import into an existing Go harness

`cmpath.local/native` is a local module name, not a registered download location. For a project whose sibling directory is the extracted `cmpath` archive, add the following to that host's `go.mod`:

```go
require cmpath.local/native v0.0.0
replace cmpath.local/native => ../cmpath/native
```

The directory layout is significant; point `replace` at the actual extracted `native` directory. An external host manages its own dependency graph and vendoring. The archive's vendor directory makes standalone builds of this module offline; Go does not automatically use a replaced dependency's vendor directory in the host module. The host must make `golang.org/x/text v0.42.0` available through its normal module cache/vendor workflow. The source repository is <https://github.com/dhava-gautama/cmpath>; the current internal module path remains compatibility-scoped until a tagged Go-module release is published.

Import `cmpath.local/native/engine`. Open a database with `engine.Open(path)` and close the returned engine when the host shuts down. Opening a new path through this direct API creates it; the stdio command separately requires `--create` for a new database.

For an existing executor, call `Begin`, `RecordModelRequest`, `StartTool`, `FinishTool` and `Commit` at the existing boundaries. For a callback workflow, use `Run(ctx, request, complete)`. The runnable example supplies concrete request, session, tool and reply types; [../docs/HARNESS.md](../docs/HARNESS.md) defines their behavior.

The engine offers native task creation/read, evidence batches/search, explicit resolution, facts, context and execution journaling. It does not implement every `TaskMemory` storage operation. Use the Python storage API for complete SQLite backups; generic legacy JSON export excludes the native journal.

## Dependency and protocol contracts

SQLite's amalgamation is in `internal/sqlite`, with exact upstream hashes in `UPSTREAM.json`. It is compiled with FTS5 and thread safety. The wrapper binds values and finalizes statements; no raw SQL command is exposed over stdio. `golang.org/x/text` supplies NFKC normalization and full case folding. Third-party license terms and Go runtime notices are included.

The base schema remains version 1 and the native journal is version 4. The wire protocol is version 1. Scoped derived snapshots carry immutable provenance edges, and `ActionEligible` issues short-lived pre-effect certificates. These certificates do not make arbitrary remote effects atomic. Changes to protocol, schema or recovery semantics require compatibility tests before a stable release. Historical 0.4.0a1 measured binaries are retained in `bin/measured-0.4.0a1`; their exact hashes are recorded in `results/native/benchmark.json`; a rebuild with different compiler/build flags can produce a different hash.

For a Windows release, place the three `.exe` files in a target-specific ZIP
under `dist/` (for example, `cmpath-native-0.4.0a5-windows-amd64.zip`). The
release verifier checks the PE/COFF symbol table and debug directory and
rejects an unstripped executable. `scripts/build_release.py` records the ZIP
alongside the wheel, sdist and portable bundle in `dist/SHA256SUMS` when it is
present.
