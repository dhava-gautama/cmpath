# Release 0.4.0a6

The current alpha adds the autosave subsystem (`cmpath.autosave`, console
script `cmpath-autosave`) for automatic, redacted, deduplicated capture of Kimi
Code session evidence, with idempotent `wire.jsonl` ingestion, atomic turn
commits, `--heal` re-ingestion, and read-only `--doctor` health checks. It
makes `check()` report database damage as a verdict rather than raising and
applies a 10000 ms `busy_timeout` floor to Python connections. The native
engine now decodes columns by SQLite storage class, reports `SQLITE_BUSY` as a
coded `busy` error, converges retention apply instead of wedging on an existing
tombstone, and declares `cmp_turns.request_id` `NOT NULL` with an idempotent
rebuild on open. It builds on 0.4.0a5's MCP adapter, authenticated managed-turn
control plane, dependency-free SDKs, explicit adapter guides, offline
conformance runner, Kimi/Hermes bundle, local doctor diagnostics, and bounded
hybrid memory router. The source is published at
<https://github.com/dhava-gautama/cmpath>. No package-index upload or hosted
service is part of this release.

## Artifacts and platform scope

The release artifacts are:

- `dist/cmpath-0.4.0a6-py3-none-any.whl`
- `dist/cmpath-0.4.0a6.tar.gz`
- `dist/cmpath-kimi-hermes-0.4.0a6.zip`
- `dist/cmpath-native-0.4.0a6-windows-amd64.zip`

The wheel contains Python code and no platform-specific native executable. The
source distribution contains the vendored native source, integration examples,
conformance suite, and portable adapter. The Kimi/Hermes ZIP is a focused
bundle containing the Python package source, launcher, and installer. The a6
cut validated a Linux x86-64 native build in place (glibc 2.39); a Windows
AMD64 set must be built in a Windows environment with MSYS2 UCRT64 GCC, and
macOS or another libc/toolchain needs its own rebuild. Do not pair an old
native executable with a newer Python package: both report their release
version through `--version`.

## Build and validate

From the project root, install Go 1.26+ and a C compiler before building native
components. Dependencies are vendored and network/toolchain downloads are
disabled by the build scripts.

```bash
python scripts/build_native.py
python -m unittest discover -s tests -v
python scripts/build_release.py
python scripts/verify_artifacts.py
python scripts/validate_release.py --go go
```

The source/payload verifier is read-only. It checks the current-version wheel,
source distribution, portable bundle and (when present) native release ZIP
against canonical source, including wheel `RECORD` hashes and PE/COFF symbol
metadata. It rejects missing, extra, or changed files that would indicate a
stale build. `build_release.py` writes deterministic archives when
`SOURCE_DATE_EPOCH` (or `--source-date-epoch`) is fixed and creates
`dist/SHA256SUMS`; the verifier checks that manifest automatically when it is
present. To verify a detached OpenPGP signature, pass `--signature PATH`
(`gpg` is only required for that opt-in check). A private signing key is never
needed for ordinary builds. When a Codex plugin checkout is available, also
pass `--plugin-root PATH --plugin-cache PATH`; pass `--plugin-artifact PATH`
for a plugin ZIP or directory. Compatibility cache entries are checked for
their stable `SKILL.md` entrypoint.

```bash
python scripts/verify_artifacts.py \
  --checksums dist/SHA256SUMS \
  --plugin-root /path/to/cmpath-memory \
  --plugin-cache ~/.codex/plugins/cache/personal/cmpath-memory
```

To sign the generated manifest with an existing local key, opt in explicitly:

```bash
python scripts/build_release.py --signing-key KEY_ID
python scripts/verify_artifacts.py --signature dist/SHA256SUMS.asc
```

`validate_release.py` then installs the wheel in a fresh virtual
environment, exercises installed CLIs and real local-tool examples, rebuilds
the source distribution's Python payload, and checks native backup/export and
replay interoperability. It requires a target-compatible native binary and a
Python 3.12+ interpreter for safe tar handling. The MCP SDK is optional and is
not installed by the default validator. When a local wheelhouse is available,
validate it in a separate environment without network access:

```bash
python scripts/validate_release.py --go go --with-mcp --mcp-wheelhouse /path/to/wheels
```

The wheelhouse must contain `mcp>=2,<3` and its dependencies. A missing or
incomplete local source records the optional check as skipped; add
`--mcp-required` when that check is a release gate. Run the standalone verifier
first when only packaging/source integrity is under review.

## Evidence and bounded claims

The 13 September SumoPod run received 80/80 fixed-reader responses and 11/11
document-agent/recovery responses. Manual review and replay/pause-resume checks
are summarized in [LIVE_RESULTS.md](LIVE_RESULTS.md). These are eight templated
synthetic workflows and two local-document tasks, not an independent-user
benchmark or a general model-quality claim; provider cost remains unknown.

The historical local retrieval study covered 40 paired cases and tied the
task-filtered SQLite/BM25 baseline at 32/40 required-source coverage. No LLM was
called in that study. The scope/provenance/eligibility safety measurements and
their residual TOCTOU limitation are in [BREAKTHROUGH_REPORT.md](BREAKTHROUGH_REPORT.md).

## Native and integration checks

Run the Python suite and Go race suite in the target environment:

```bash
CMP_NATIVE_BINARY="$PWD/native/bin/cmpath-native" python -m unittest discover -s tests -v
cd native
go test -buildvcs=false -race -count=1 ./...
```

The doctor command performs local, side-effect-bounded checks without installing
packages, starting an MCP server, opening a native database, or replacing user
files:

```bash
cmpath doctor --json --skip-native --skip-mcp
```

The integration conformance runner is offline and uses an in-memory SQLite
fixture:

```bash
python conformance/run.py
```

MCP clients, native harnesses, the control plane and the five-hook adapter
guides are documented in [integrations/README.md](../integrations/README.md).

## Historical studies

`PILOT_RESULTS.md`, `results/pilot`, the baseline PDF/workbook,
`NATIVE_ARCHITECTURE.md`, and `results/native` retain earlier
measurements. Their release labels and test counts are historical evidence and
must not be presented as fresh a6 runs. The 0.3 public-data study has no model
answer-quality measurement.

## Publication handoff

The public source repository is `dhava-gautama/cmpath`. No package-index
publication, tagged Go-module release, deployment, or hosted service is
claimed. Before publishing release assets, build each supported platform,
regenerate checksums, and run the complete validator in each target
environment. A checksum authenticates bytes against a manifest; it does not
establish publisher identity. A detached signature adds publisher-key
verification only when the recipient separately validates its trusted public
key.
