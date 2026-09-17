# CMP for Kimi CLI and Hermes Agent

This bundle adds CMP as a local MCP memory server. It does not configure or replace the model, endpoint, login, or API key used by Kimi or Hermes.

Extract it to a stable directory, then run `bash portable-kimi-hermes/install.sh`.

The installer preserves other configuration entries and creates separate databases under `~/.local/share/cmpath/`.

MCP exposes CMP memory tools. Full managed-turn fencing still requires a native host adapter implementing CMP's five lifecycle hooks.
