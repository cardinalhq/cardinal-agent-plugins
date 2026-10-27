# cardinal-agent-plugins

Source of truth for Cardinal's agent plugins and their shared core.

One contract, written once, shipped to every agent runtime:

```
core/cardinal_core/     shared runtime — OTLP emission, initiative
                        resolution, spend limits, device consent,
                        session counters
adapters/               per-agent surfaces (codex, gemini, cursor,
                        claude, copilot, opencode, pi, devin)
build/vendor.py         copies core into each plugin artifact so shipped
                        plugins stay self-contained (no pip for users)
docs/specs/             the extraction spec and migration plan
```

## Status

P0–P4 code-complete: core extracted (52 unit tests) and all four adapters
migrated with byte-equal golden parity against their shipped plugins
(~226 adapter tests + a cross-adapter contract test in `tests/`). See
`docs/specs/agent-core.md` for the plan and
`docs/specs/core-gaps-followup.md` for the core 0.2.0 reconciliation.

The Claude adapter also ships an auto-installed Invariant `PreToolUse`
hook (`adapters/claude/hooks/invariant-check.py`) — advisory-only, fires
before Edit/Write/MultiEdit on code covered by an Invariant guarantee.

Claude also ships opt-in decision capture (`cardinal-decision on`): a
`UserPromptSubmit` hook asks the agent to record the choices it makes with
`bin/cardinal-decision`, and each one is sent as a `cardinal.decision` event
tagged with engineer, session, repo, branch, PR, anchors, and D18 code
clusters. The model-invoked `/cardinal:decision` skill answers "is decision
capture on?" and turns it on or off by running `cardinal-decision`. See
[docs/specs/decision-telemetry.md](docs/specs/decision-telemetry.md).

Before `/cardinal:connect` the Claude plugin is local-only: its `cardinal` MCP
server has no URL and never connects, evidence capture keeps every tool call's
result on the machine, and every telemetry, spend-limit, initiative, plan,
decision, git-state and usage hook stays silent (no context, no network).
Writing to Cardinal needs an API key (sign up at https://app.cardinalhq.io,
then `/cardinal:connect` creates one for the machine); there is no OAuth sign-in. See
[adapters/claude/README.md](adapters/claude/README.md).

A self-hosted Cardinal behind a certificate the machine does not trust
(self-signed, private CA) works in every adapter: connect probes the host
first and, only then, shows the certificate (subject, names, validity,
SHA-256 fingerprint) and asks; `--trust-cert <sha256>` pins exactly that
certificate for that hostname under `~/.cardinal/tls/`, which every hook,
script and skill then verifies against (`core/cardinal_core/tls.py`; nothing
else is weakened). Claude also gets `NODE_EXTRA_CA_CERTS` for its own MCP
client and telemetry; the other runtimes' own clients are told what to set.
See [docs/specs/tls-trust.md](docs/specs/tls-trust.md).

Connected, every Claude session automatically has an Investigation and a
private live Investigation Storyboard on Cardinal (session id → investigation
id → storyboard id → URL), created at session start by
`hooks/storyboard-session.py` with no command or skill; ask Claude for the
storyboard link at any time. Advisory events posted to the investigation (cue,
question, challenge) reach the session at its next tool boundary through a
per-session background poller, so a tool call never waits on the network.
When its understanding materially changes, the session checkpoints it to the
same ordered stream (`cardinal-storyboard investigation checkpoint`: sparse,
batched hypothesis / experiment / finding / decision / question events, each
the agent's claim, never a fact or owner authority). A Cardinal that
advertises `capabilities: {projection: {enabled: true}}` keeps the live
storyboard up to date from those checkpoints itself, and the session is told no
storyboard step is needed; any other keeps the skill-driven flow below. Events
are delivered and listed by the `class` Cardinal stamps on each one (`control`,
`semantic`, `owner_input` or `owner`; an older Cardinal without it: by type); an event
of a class this plugin does not know, and owner input, is never delivered to
the model. Only when Cardinal advertises `capabilities: {owner_input:
{enabled: true}}` does the author session record the owner's prompts as owner
input (`hooks/owner-input.py`: credentials scrubbed, up to 32 KiB, readable only
by the author and grantees with `owner_input:read`; shown to the user before
capture starts; `CARDINAL_OWNER_INPUT=0` in `~/.claude/settings.json` `env`
turns it off on this machine). `cardinal-storyboard investigation grant`
gives someone else scoped, revocable access to the Investigation through
`CARDINAL_INVESTIGATION_TOKEN`, never this machine's key.
The shared CLI also supports supervisor follow reads, compact output, structured
checkpoint artifacts and local hash verification, and receipt lookup. With the
`owner_decision` capability, decisions posted from the investigation author's
signed-in Cardinal web session reach each author's session as the owner's own
words; in another human's joined session they remain advisory. See
[the supervision CLI contract](docs/specs/supervision-cli.md) for cursor, grant,
capability, compatibility and recording-failure behavior.
At the completion of investigation work, connected agents offer to update the storyboard visualization. With the user's consent, the same session authors and previews it. Codex, Cursor and Gemini include the `cardinal-storyboard` skill and a local preview renderer; Claude uses its storyboard and canvas skills. Declining or completing an update does not trigger another offer. Publishing and sharing remain separate explicit requests. Deploy the companion Conductor change to remove UI/server visualization generation.

Claude also ships two model-invocable skills for Cardinal's Investigation
Storyboards: [`/cardinal:storyboard`](adapters/claude/skills/storyboard/README.md)
improves, frames and publishes that live storyboard (evidence-bound, scene by
scene) through the `storyboard__*` MCP tools, and
[`/cardinal:canvas`](adapters/claude/skills/canvas/README.md) draws each
scene's visual and previews it. `storyboard__preview` returns one
self-contained page per scene. A synchronous `PostToolUse` hook on
`storyboard__preview` (`hooks/storyboard-preview.py`) passes the result to
the storyboard skill's `scripts/render_preview.py --runtime claude`, which
renders each page in the user's own Chrome/Chromium, over the DevTools pipe,
with the sandbox on and the network locked; the hook hands Claude the PNG
paths (and any per-scene render errors) to Read. `render_preview.py` stays
the manual fallback (dark theme, a subset, a timed-out run). Nothing is
rendered server-side, and publish trust is deterministic. Local preview
supports macOS and Linux, not Windows. A synchronous `SessionStart` hook
(`hooks/storyboard-session.py`) puts the session id, the investigation and
the live storyboard URL in context. The canvas skill is Claude-only for now.

Every adapter ships generic local evidence capture for storyboards: every
tool call the agent makes (shell commands, file reads and edits, searches,
web fetches, subagents, any MCP server, and tools nobody has listed yet) goes
through ONE pipeline, `core/cardinal_core/evidence_capture.py`
(`capture_call`), into `~/.cardinal/evidence/<session_id>/ev_<id>.json`
(directories 0700, files 0600). The capture decision never names a tool: an
opt-out check, a skip for Cardinal's own gateway (it already mints witnessed
receipts), and a tool-neutral sensitivity gate
(`core/cardinal_core/evidence_gate.py`) over every string of the call's
input. A call that touches a sensitive path (`.env`, keys, `~/.aws`,
kubeconfig, …), runs a secret-dumping command (`printenv`, `gh auth token`,
`kubectl get secret`, …) or sends a credential is kept only as a *withheld*
stub (tool, status, rule; no input, no output) so the agent can say why it
cannot cite it. Everything else is shaped by optional, shape-based
normalizers (`core/cardinal_core/evidence_normalizers/`: MCP content, shell
output, file edits; a generic JSON fallback for the rest), scrubbed (the
gateway's receipt scrub plus plain-text `key=value` rules, base64 blobs and
local paths) and capped at 256 KiB. The spool stays under 14 days, 256 MiB
and 10,000 entries per session. Nothing leaves the machine: only a result
cited in a storyboard is uploaded, by an explicit
`cardinal-evidence promote --storyboard sb_… ev_…`
(`core/cardinal_core/evidence_promote.py`, launched per adapter), as a
*captured* receipt (`source_server` is the MCP server, or
`builtin:<runtime>` for the agent's own tools). Claude authenticates the
upload with the storyboard's own 24 h evidence token
(`hooks/storyboard-token.py`) and falls back to the Cardinal MCP key; the
other adapters use their connection's MCP key. Opt out with
`cardinal-evidence off`, `CARDINAL_EVIDENCE_CAPTURE=0` or the flag file
`~/.cardinal/evidence/disabled`; `CARDINAL_EVIDENCE_CONTEXT=0` keeps
capturing but stops the inline `[evidence:…]` lines; extra deny (and, in
your home only, allow) rules go in `~/.cardinal/evidence-rules.json`. See
[docs/specs/generic-evidence-capture.md](docs/specs/generic-evidence-capture.md)
for the per-runtime hook wiring.

The [GitHub Copilot CLI](adapters/copilot/README.md) plugin installs from its mirror's
plugin marketplace (`copilot plugin marketplace add cardinalhq/cardinal-copilot-plugin`,
then `copilot plugin install cardinal@cardinal-copilot-plugin`). Its hooks ship in the
plugin and stay silent until `scripts/cardinal-connect`; it has the Cursor adapter's
shape (no prompt-time context, no turn-end block) and no token usage. It has not yet
been exercised in a live Copilot session.

Native [OpenCode](adapters/opencode/README.md) and [Pi](adapters/pi/README.md)
packages provide session/tool/usage telemetry and Cardinal MCP access. Build
them with `python3 build/native.py`; test with `npm ci --ignore-scripts` and
`npm run test:native`. See [native package releases](docs/RELEASING-NATIVE.md)
for installable artifacts, compatibility targets, and the backend runtime
filter needed before production session analytics can consume their usage.

[Devin](adapters/devin/README.md) has no plugin surface, so its adapter is a
server-side poller over the Devin REST API. It sends `cardinal.git_state`
with PR linkage and, for sessions created with the Cardinal
structured-output schema, `cardinal.decision`. It has not yet been run
against a live Devin org. It ships as a release tarball and the
`ghcr.io/cardinalhq/cardinal-devin-poller` image on `devin-vX.Y.Z` tags; see
[docs/RELEASING.md](docs/RELEASING.md#3-devin-poller).

## Release flow

Development happens here; each adapter is published to a per-CLI **release
mirror**, where users install from. Install URLs and marketplace slugs keep
working; the mirrors are build outputs, not sources.

- [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)
- [cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
- [cardinal-cursor-plugin](https://github.com/cardinalhq/cardinal-cursor-plugin)
- [cardinal-gemini-plugin](https://github.com/cardinalhq/cardinal-gemini-plugin)
- [cardinal-copilot-plugin](https://github.com/cardinalhq/cardinal-copilot-plugin) (GitHub Copilot CLI; also its plugin marketplace)

Bumping an adapter's `plugin.json` version on `main` triggers
`.github/workflows/release-mirrors.yml`, which runs `build/release.py` to
push the built artifact (adapter code + vendored `cardinal_core`) plus a
`vX.Y.Z` tag to that adapter's mirror, syncing `marketplace.json` so the CLI
offers the update. `release.py` is idempotent — mirrors already at the tag
no-op.

The shared core ships to PyPI separately (`.github/workflows/release.yml`,
via OIDC trusted publishing) on `core-vX.Y.Z` tags as `cardinal-agent-core`.

The omnigent policy adapter (`cardinal-omnigent-policy`) was removed on
2026-09-14. Its published PyPI releases (up to 0.4.0) stay available but get
no further updates.

## Development

```bash
cd core && python3 -m unittest discover tests -v   # core suite
python3 build/vendor.py --all                       # vendor core into adapters
```

The Python runtime supports Python 3.9+ and uses only the standard library.
The OpenCode and Pi packages also require Node; Pi's MCP client has npm
dependencies. See their adapter READMEs for supported runtime versions.

## License

Apache 2.0. See [LICENSE](./LICENSE).

## Repository associations across a session

Repository provenance is shared across Claude Code, Codex, Cursor and Gemini.
Starting in a parent workspace does not prevent later repository attachment:
explicit tool working directories, file arguments and successful edits add
repository labels locally. The background poller submits them to Cardinal.
The original checkout context remains unchanged; later repos are additive
`written_from` provenance, never automatically asserted as the storyboard's
subject. Labels contain canonical repository names, never local paths.

To backfill an existing session from its saved edited-file ledger, run:

```sh
cardinal-storyboard investigation repos --session SESSION_ID
```

Supply `--cwd /path/to/checkout` (repeatable) to attach additional checkouts.
This command uses the session's existing investigation and storyboard. It does
not publish or share anything. Automatic collection respects
`CARDINAL_STORYBOARD_CONTEXT=0`, and only an author's own session may sync.
At most 32 repositories are retained. Shell command text is not parsed: use
explicit tool working directories or the CLI for shell-only checkout changes.
Historical repair requires a retained local edit ledger or explicit checkouts.

Multiple labels and browser grouping require Cardinal's
`repository_provenance` capability (the companion Conductor migration/server
change). Older servers continue to accept the existing context request, and
the CLI reports when full multi-repo support is unavailable. Every adapter's
next patch release includes this same shared implementation; the parity matrix
and packaged-artifact tests guard against adapter drift.

## Coordination on agent intent

Supervisor and worker can coordinate at any point in their work: use
`investigation sync --intent PURPOSE` to catch up, declare a required result with
`request --intent PURPOSE --assignee ...`, and `wait --intent PURPOSE` to resume
that purpose when feedback or a result needs attention. Exact prerequisites
survive restarts; only exact complete-result acceptance or reasoned cancellation
resolves a dependency. Blocked/abstain/reject, reading and timeout never release it.

Ordinary tools are not classified or intercepted. There is no fixed action list,
mode, policy file or enable ceremony. Waiting is a bounded invocation with
incremental reads/backoff, not server push or indefinite host parking. Deploy
intent-capable backend/protocol 2 before new clients and upgrade participants
sharing an Investigation before creating intent-only records. See the
[runtime contract](docs/specs/coordination-gate-runtime.md) and
[request commands](docs/specs/coordination-requests.md) for semantics and limits.
