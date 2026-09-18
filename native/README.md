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

`engine` embeds `base.sql` and `journal.sql`, and the schema a database is created with must not depend on the machine that checked the code out. The root `.gitattributes` keeps `*.sql` at LF on every platform, and `lineEndingsToLF` converts any CRLF or lone CR back to LF before the scripts are executed, so the checkout style cannot reach SQLite. A `.sql` file is still recognized as text (`text=auto`): normalizing is what keeps the two behaviours consistent, and it is why a `.sql` payload is never marked binary.

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

The base schema remains version 1 and the native journal is version 4. Journal schema 4 declares `cmp_turns.request_id` `NOT NULL`, because a `TEXT PRIMARY KEY` on a rowid table is nullable in SQLite; `Open` rebuilds a database written before that constraint, and `info` reports `cmp_turns_request_id_not_null` so the two states can be told apart. The version is unchanged because the constraint only removes states the schema already admitted: an older reader remains correct, and a bump would make every older binary refuse a database it can still read. The wire protocol is version 1. Scoped derived snapshots carry immutable provenance edges, and `ActionEligible` issues short-lived pre-effect certificates. These certificates do not make arbitrary remote effects atomic. Changes to protocol, schema or recovery semantics require compatibility tests before a stable release. Historical 0.4.0a1 measured binaries are retained in `bin/measured-0.4.0a1`; their exact hashes are recorded in `results/native/benchmark.json`; a rebuild with different compiler/build flags can produce a different hash.

For a Windows release, place the three `.exe` files in a target-specific ZIP
under `dist/` (for example, `cmpath-native-0.4.0a6-windows-amd64.zip`). The
release verifier checks the PE/COFF symbol table and debug directory and
rejects an unstripped executable. `scripts/build_release.py` records the ZIP
alongside the wheel, sdist and portable bundle in `dist/SHA256SUMS` when it is
present.

## Storage classes, error codes and lock contention

`internal/sqlite` decodes each column by its SQLite storage class: `NULL` to
nil, `INTEGER` to `int64`, `REAL` to `float64`, `TEXT` to `string` and `BLOB`
to `[]byte`. A blob is never handed back as text. That coercion made a blob
request ID indistinguishable from a text one, and SQLite never compares a blob
equal to text, so rebinding the coerced value matched no row and the affected
maintenance statement looked like a successful no-op. A column whose class the
driver cannot represent fails the query instead of being guessed.

`Row.Text`, `Row.Int`, `Row.Float` and `Row.Blob` read one column and return a
coded `*sqlite.TypeError` for a NULL, absent or wrongly-typed column. Prefer
them over a bare type assertion on a column that is not NOT NULL in the schema.
`Engine.Code` classifies an unrepresentable column as `integrity`.

`Engine.Code` keeps lock contention separate from storage damage: a
`SQLITE_BUSY` result, including the extended `SQLITE_BUSY_SNAPSHOT` and
`SQLITE_BUSY_RECOVERY` variants, is `busy`, which a caller may retry. Any other
failure that is not an `engine.Error` remains `storage_error`. Nothing retries
automatically, and the stdio bridge answers a recovered panic as
`internal_error` so one bad row cannot end the host's persistent channel.

Every connection installs a busy-timeout floor of 10000 ms. WAL readers never
block, but two writers serialize on one file, and a single maintenance
transaction exceeds the 5 s default this wrapper installed earlier: a
160k-message export measured about 5.2 s, a 50k-row `AppendBatch` about 12.0 s,
a 20k-turn `RetentionPlan` about 8.4 s, a 20k-turn `ApplyRetention` about
10.8 s, and a throttled export held the write lock for 311 s. Below the floor
the collided writer returns "database is locked" rather than waiting. Raise the
floor where a larger transaction is routine; a caller may ask for a longer
wait, never a shorter one:

```go
memory, err := engine.Open(path, engine.WithBusyTimeoutMS(60_000))
```

`cmpath-native --busy-timeout-ms` sets the same value for the stdio command.
`DB.Changes()` reports the rows modified by the last statement on a connection,
for a caller that must tell an effective statement from one that matched
nothing.
