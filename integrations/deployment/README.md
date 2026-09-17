# CMP conformance deployment

These artifacts package and run the offline adapter contract. They do not
contact a model provider, require credentials, or expose a network listener.
The reference fixture uses only Python's standard library and an in-memory
SQLite database.

From the `cmpath` directory:

```bash
# POSIX / WSL
sh integrations/deployment/run.sh

# Windows PowerShell
pwsh -File integrations/deployment/run.ps1

# Container smoke test
docker build -f integrations/deployment/Dockerfile -t cmpath-conformance .
docker run --rm cmpath-conformance
```

The runner exits nonzero on a contract failure. `conformance/SPEC.md` is the
normative contract and `conformance/spec.json` is the machine-readable case
index. A native executable is intentionally not required; when a caller has a
platform-built native adapter, it can pass that adapter to `run_suite` for an
additional smoke run. Supplied binaries are platform-specific and must not be
copied between Windows, macOS, and Linux images.

