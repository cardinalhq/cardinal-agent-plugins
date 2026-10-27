---
name: cardinal-connect
description: Connect GitHub Copilot CLI to Cardinal by running the device-code flow and configuring Cardinal telemetry plus the unified Cardinal MCP server.
---

# Cardinal Connect

Use this skill when the user asks to connect Copilot CLI to Cardinal, enable Cardinal telemetry, enable Cardinal MCP tools, rotate a Cardinal connection, or run Cardinal setup.

The script is `scripts/cardinal-connect` in the Cardinal plugin directory (two levels up from this skill's directory). Run it with that absolute path, not a similarly named file in the workspace:

```bash
python3 <plugin directory>/scripts/cardinal-connect
```

The script calls Cardinal and writes `~/.copilot/mcp-config.json` (`$COPILOT_HOME/mcp-config.json` when set) and `~/.copilot/cardinal*.json`. **Sandbox.** If Copilot CLI's command sandbox is on (`/sandbox enable`; it is off by default), a shell command has no network and cannot write outside the workspace, so this script fails inside it: ask the user to approve a bypass for this one command, or give them the exact command to run in their own terminal.

If the user asks for a non-production Cardinal host, pass `--host <url>`. If the script reports that Cardinal is already connected, ask whether to rotate, or rerun with `--rotate` when the user has already asked to overwrite.

Besides the ingest and MCP keys, the script requests a control-plane token (`maestro:act`) with `dashboards:write`, `alerts:write` and `telemetry:query`, so skills like migrate-from-grafana can write dashboards and alert rules and query telemetry as the user. That is bounded by their org role, and `telemetry:query` is read-only in every org they belong to. Mention that when you show the approval URL. The token is stored 0600 in `~/.copilot/cardinal-secrets.json`, and `cardinal-disconnect` revokes it. Pass `--minimal-scopes` for users who don't want it.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to start a new Copilot CLI session (or `/restart`) so it loads the Cardinal MCP servers and the connected hooks, then suggest the status skill.

## A self-hosted Cardinal behind a self-signed certificate

Most Cardinal hosts carry a certificate the machine already trusts, and the script says nothing about TLS. When `--host` presents one it does not trust (self-signed, or a private CA), the script stops **before** the device flow with exit status 3 and prints the certificate: the verification error, subject, issuer, the names it carries (and whether they include the host), validity and its SHA-256 fingerprint, plus the exact re-run command. Show all of that to the user and ask whether they expected it (they run this Cardinal) and want to trust exactly that certificate. Never answer for them. If they say yes, re-run with the fingerprint the script printed:

```bash
python3 <plugin directory>/scripts/cardinal-connect --host <url> --trust-cert <sha256 fingerprint>
```

That pins the certificate for that hostname in `~/.cardinal/tls/` (delete the file to forget it): every Cardinal hook, script and skill then verifies the host against it, and nothing else is weakened. If the fingerprint does not match what the server presents on the re-run, the script refuses. Copilot CLI's own MCP client needs `NODE_EXTRA_CA_CERTS=~/.cardinal/tls/ca-bundle.pem` exported in the shell that launches it; the script prints that line. It also checks that the certificate names the host; the script says when it does not. See `docs/specs/tls-trust.md`.

## Owner input (the user's own decision)

`cardinal-connect --owner-input on|off` does not connect. It decides whether this machine's Copilot CLI sessions record the prompts the user types as owner input of their Cardinal Investigation. It prints the full text being consented to, and `on` asks the user to type `yes`.

Never run `--owner-input on` yourself, and never suggest piping an answer into it: the consent is the user's, given in their own terminal. If the user asks about it, explain what it does and give them the command to run. `--owner-input off` stops it. `CARDINAL_OWNER_INPUT=0`, or `{"copilot": "off"}` in `~/.cardinal/owner-input.json`, turns it off whatever the consent says.

Do not claim Copilot CLI's own OpenTelemetry export was enabled. The plugin emits Cardinal-compatible telemetry from Copilot CLI hooks.
