# Connecting to a Cardinal host with an untrusted TLS certificate

**Status:** implemented (core `cardinal_core.tls`, every adapter's connect).

## Why

Most Cardinal sites (app.cardinalhq.io, customer sites behind a real
certificate) are trusted by the machine already, and nothing here applies to
them. A self-hosted Cardinal behind a self-signed or private-CA certificate
is different: every HTTPS call the plugins make goes through Python's
`urllib` with default verification, so `cardinal-connect` fails at the first
request (`CERTIFICATE_VERIFY_FAILED`) and no hook could reach the host even if
connect had succeeded. The runtime's own clients (Claude Code's MCP client
and OTel exporter, Codex's MCP client, …) verify too.

A customer's ALB, for example, presented a self-signed certificate whose
subject and SANs (`cardinal.test`, `*.cardinal.internal`) did not even name
the ALB hostname. The device-code login itself worked once TLS verification
was out of the way: trust was the only blocker.

## What connect does

1. **Probe first, ask only when needed.** Before the device-code request,
   connect performs a TLS handshake with `--host`. A certificate the system
   trusts (or one already pinned) passes silently: the common case never sees
   a question.
2. **Show the certificate, then ask.** When verification fails, connect
   fetches the leaf certificate without verifying and prints what it is:
   the verification error, subject, issuer, the names it carries (and whether
   they include the host), validity, and its SHA-256 fingerprint.
   - On a terminal (stdin is a TTY) it asks `Trust this certificate for
     <host>? [y/N]`.
   - Driven by an agent (no TTY) it exits with status **3** and tells the
     agent to show the fingerprint to the user and, with their consent,
     re-run with `--trust-cert <sha256>`. The fingerprint on the re-run must
     match what the server presents then, so a certificate swapped between
     the two runs is refused.
3. **Pin exactly that certificate for that hostname.** Trusting writes
   `~/.cardinal/tls/<hostname>.pem` and rebuilds `~/.cardinal/tls/ca-bundle.pem`
   (every pinned certificate in one file, for runtimes that take a CA
   bundle). Nothing else is weakened: other hosts keep full verification.
4. **Check the other hosts the bundle names.** The ingest endpoint, MCP URL
   and act endpoint may sit on another hostname; each is probed the same
   way before the reachability checks. `--trust-cert` can be given more
   than once.

## How the plugins honor a pin

`cardinal_core.tls` installs a process-wide `urllib` opener whose HTTPS
handler, per request, looks the request's hostname up in `~/.cardinal/tls`.
For a pinned host it verifies the server against **only** the pinned
certificate (`CERT_REQUIRED`, trust store = that certificate,
`VERIFY_X509_PARTIAL_CHAIN` where Python offers it) with hostname checking
off, since a self-signed certificate often does not name the host it is
served from. For any other host it is the default handler. The install
happens when `cardinal_core` is imported, so every hook, CLI and skill
script that imports the core (every adapter vendors it) gets it without
code; the few places that build their own opener (`build_opener`) add
`tls.https_handler()`.

A pinned certificate that stops matching (the server rotated it) fails
verification like any untrusted certificate; connect then shows the new
certificate and asks again.

## The runtime's own clients

The plugins cannot change how each agent runtime verifies TLS, but connect
configures what it can and says the rest:

| Runtime | Own HTTPS clients | What connect does |
|---|---|---|
| Claude Code | MCP client, OTel exporter (Node) | Writes `NODE_EXTRA_CA_CERTS=~/.cardinal/tls/ca-bundle.pem` to `~/.claude/settings.json` `env` (documented by Claude Code for custom CAs) unless the user already set their own. With `--insecure-runtime-tls`, also `NODE_TLS_REJECT_UNAUTHORIZED=0`. Both are removed by disconnect when connect set them. |
| Codex | MCP client (Rust, system trust store) | Prints how to add the certificate to the system store. |
| Gemini CLI, Copilot CLI | MCP client, OTel (Node) | Prints the `NODE_EXTRA_CA_CERTS` export to set before launching. |
| Cursor | MCP client (Electron) | Prints the system-store / launch-environment note. |
| OpenCode, Pi | MCP client (Bun / Node) | Prints the `NODE_EXTRA_CA_CERTS` export. |
| Devin poller | none against Cardinal besides core | Core opener only. |

`NODE_EXTRA_CA_CERTS` adds a trust anchor but **keeps hostname
verification**, so it only helps when the certificate names the host.
Connect checks the SANs and says so when they do not match: the fixes are
a certificate that names the host, reaching Cardinal by a name the
certificate carries, or (Claude only) `--insecure-runtime-tls`, which turns
off TLS verification for every HTTPS connection Claude Code makes
(including to Anthropic) and is printed with that warning.

## Files

- `~/.cardinal/tls/<hostname>.pem` — one pinned certificate per hostname.
  Delete the file to forget it.
- `~/.cardinal/tls/ca-bundle.pem` — all pinned certificates, rebuilt on
  every pin.
- Connect state (`cardinal.json`) records `tls.pinned_hosts` and, for
  Claude, which env keys connect set, so status can report them and
  disconnect can remove only what connect added.

## Found alongside: relative URLs from a Cardinal without MAESTRO_BASE_URL

The same customer's Cardinal runs without `MAESTRO_BASE_URL`, so its device
flow answers with paths instead of URLs (`verification_uri: /connect?code=…`,
`mcp.url: /api/orgs/<id>/mcp`; conductor's `device-auth.ts` builds them that
way when the base URL is null). The plugins used to print the bare path as
the approval link and then crash probing the MCP URL. `deviceflow.resolve_url`
now resolves any leading-slash URL in the grant or the bundle against
`--host`, in core, so every adapter inherits it.

## Out of scope

- A private-CA bundle with hostname checking on (`--ca-cert`): the pin covers
  that case too (the leaf is pinned), just more tightly.
- Changing the runtimes' own MCP clients (Pi's is the one we ship, but Node's
  `fetch` exposes no public per-request CA option).
