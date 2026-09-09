# OpenCode and Pi packages

`adapters/opencode` and `adapters/pi` are npm/Pi packages. Their source of
truth is this repository; they do not use the legacy four release mirrors.
`build/native.py` creates self-contained artifacts with the shared
`common/native` boundary and a byte-for-byte vendored `cardinal_core`.

## Build and verify

```sh
npm ci --ignore-scripts
npm run test:native
python3 -m unittest discover tests -p test_native.py -v
python3 -m unittest discover tests -p test_contract.py -v
cd core && python3 -m unittest discover tests
```

The native suite checks published upstream types (OpenCode 1.18.30, Pi 0.85.1),
the real Pi extension loader, normalized events through Python into an OTLP
HTTP receiver, persistent deduplication, failures, cross-workspace isolation,
MCP initialize/list/call/error behavior, pagination and redirect isolation.
It also packs and installs both artifacts outside the checkout and loads
the installed entry points. No user credentials or model calls are needed.

`native-adapters.yml` runs this on Node 24 with Python 3.9 and 3.12 and uploads
both tarballs. Tags `opencode-vX.Y.Z` and `pi-vX.Y.Z` run the same checks and
publish a GitHub release with the matching tarball after both Python matrix
jobs pass. The tag must match that adapter's `package.json` version. Local
testing also works on supported Node versions. Build
directories are disposable; do not configure long-lived installations against
one that a concurrent build may replace.

## Package and publish

From the repository root:

```sh
python3 build/native.py
npm pack ./dist/native/opencode --pack-destination ./dist/native
npm pack ./dist/native/pi --pack-destination ./dist/native
```

Artifacts are `cardinalhq-opencode-plugin-0.1.0.tgz` and
`cardinalhq-pi-plugin-0.1.0.tgz` under `dist/native`. They can be installed
directly with npm before publication. Each adapter's README documents its
runtime installation and connection commands.

To make a GitHub release, push a matching tag from the reviewed commit:

```sh
git tag opencode-v0.1.0
git tag pi-v0.1.0
git push origin opencode-v0.1.0 pi-v0.1.0
```

Each tag's workflow builds and tests both packages, then attaches only the
matching package to its GitHub release. Release publication starts as a draft
and becomes public only after the package is uploaded. A retry preserves an
already published release.

With npm publishing credentials for the `@cardinalhq` scope, separately publish the
reviewed tarballs using `npm publish <artifact.tgz> --access public`.
GitHub releases do not publish to the npm registry or change runtime configuration.
Future releases increment the adapter's
`package.json` version. Build stamps that version into bridge invocations.
Do not run `npm pack` in an unbuilt source adapter: its prepack check fails
rather than silently shipping a package without Python code.

## Backend rollout dependencies

The adapter contract uses `service.name` and `agent.runtime` values `opencode`
and `pi`. Raw OTLP ingestion accepts those values. **Session analytics requires
the matching Lakerunner change before users are onboarded.** The inspected
backend currently limits its agent-session tap to the existing five runtimes:

- In `internal/agentsessions/lkrn_tap.go`, extend the service-name selector
  from `^(claude-code|codex|cursor|gemini|omnigent)$` to
  `^(claude-code|codex|cursor|gemini|omnigent|opencode|pi)$` and extend its
  selector regression test.
- In `internal/agentsessions/identity`, add explicit dispatch for both
  runtimes. The new `tool_result` carries `tool_name`, `mcp_server_name`, and
  `mcp_tool_name` directly. Attribute MCP calls from those fields and ordinary
  tools from `tool_name`. Process `tool_result` once; also consuming
  `cardinal.turn_tool` would double-count the same call.
- Verify runtime selectors and labels in downstream analytics/UX and run a
  connected session from each agent. Check model usage, failed tools, initiative
  attribution, and MCP capability identity against ingested rows.

Those are backend changes in the Lakerunner repository, not performed by
installing a plugin. The plugins are tested against the existing shared event
contract; local sink tests do not claim production session-analytics acceptance.

Cardinal's device-auth route accepts arbitrary client labels, so
`cardinal-opencode-plugin` and `cardinal-pi-plugin` do not need an authentication
allowlist change. The client requests only `ingest:write` and `mcp:invoke`.
MCP keys support self-revocation; ingest keys use a different backend table and
require revocation through the API-key settings UI. Disconnect explicitly reports
this and leaves a credential-free receipt with the outstanding key IDs.

## Initial feature boundary

Both adapters emit git state, model usage, and successful/failed tool activity.
OpenCode supplies native remote MCP; Pi ships a lazy MCP client with discovery
and call tools. Neither adapter implements spend enforcement, subscription-plan
reporting, skill/command attribution, or aggregate subagent spend in 0.1.0.
OpenCode V2 and the former `@mariozechner` Pi distribution require separate
compatibility verification before they are advertised as supported.
