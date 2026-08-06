# runtime-comms-plan v0.2

**Status:** Phase 1 shipped and in-tree. Phase 2 named, not built.

This document is the referent for the four in-code claims that previously
pointed at nothing:

| Reference | What it asserts |
|---|---|
| `common/deployment-schema.yaml:7` | this plan (with `sentinel-lint-plan v0.3`) holds the authoritative `deployment.yaml` shape |
| `spike/executor/deployment.py:3` | `deployment.yaml` is "Phase 1 of the runtime-comms plan" — the sidecar binding capabilities + operator-comms rails to providers |
| `spike/executor/capabilities.py:217` | the provider registry is "Phase 1 of runtime-comms plan" |
| `spike/executor/lint.py:159` | `kind: Variation` is refused pending "the v1 overlay-bindings story" |

Everything below is a description of code that exists in this repo at the time
of writing, plus an explicitly-labelled section for what does not. Where the
two disagree, the code wins and this document is the bug.

---

## 1. The problem this solves

A compiled Sentinel (`sentinel.yaml`) is immutable and portable: it names
**abstract capability ids** (`observability.query-logs`), not MCP tool names,
endpoints, or credentials. That is what makes the same DAG runnable against a
different backend.

Something therefore has to supply the concrete half — which provider, which
endpoint, which token, which Slack channel, which findings sink — and it has to
do so *per deployment site*, without editing the immutable artifact and
invalidating its digest.

That something is `deployment.yaml`, the **runtime-comms sidecar**. It sits
next to `sentinel.yaml` in the Sentinel directory and carries nothing but
bindings. It deliberately has no `metadata.name`: the name is derived from the
sibling `sentinel.yaml` so there is no copy-paste bait
(`spike/executor/deployment.py:5-6`).

## 2. The four rails

`deployment.yaml` binds four independent rails. Each has the same shape — a
registry in the executor, an id in the sidecar, resolution at node-execution
time — and each is dispatched from one place, `runtime_serve._run_node`
(`spike/executor/runtime_serve.py:4-10`):

| Rail | Sidecar key | Executor registry | Registered today |
|---|---|---|---|
| Capabilities (tool nodes) | `capabilityBindings` | `capabilities.resolve_provider(cap, provider)` | `mcp` (4 observability ids), `fixture` (6 ids) |
| Operator comms (`ask_human` nodes) | `askHumanBindings` | `channels.resolve_channel(channel_ref)` | `cli-prompt`, `slack.socket-mode`, `test.mock` |
| Findings out | `findingsRouting` | `sinks.resolve_sink(sink_id)` | `stdout` |
| Secrets | `credential_ref` etc. | `secrets.resolve("<scheme>://…")` | `env://` only |

Two more binding groups exist in the schema and are validated but are not part
of the capability seam described below: `inputBindings` (where each declared
input's value comes from) and `llmBindings` (model + token budget per llm
node). In-cluster llm execution is out of scope for Phase 1 — the dogfood
sentinel has no llm nodes.

`secrets.resolve` refuses `k8s-secret://`, `vault://` and `aws-sm://` by name,
pointing at Phase 2, rather than silently returning nothing
(`spike/executor/secrets.py:29`).

## 3. The capability-binding contract (the CR → executor seam)

This is the load-bearing part of v0.2 and the part that was broken until the
seam fix in this change. There are **five hops** and every one of them is
pinned by a test.

### 3.1 Hop 1 — the CR declares a LIST

The `Sentinel` CRD carries `spec.capabilities` as a list of
`{id, provider, endpointSecretRef?, tokenSecretRef?}`; `id` and `provider` are
both `required` at the API-server level (`k8s/crds/sentinel.yaml:142-174`).
The Secrets are referenced by name, never inlined.

```yaml
spec:
  capabilities:
    - id: observability.list-services
      provider: mcp
      endpointSecretRef: cardinal-mcp-endpoint
      tokenSecretRef: cardinal-mcp-token
```

### 3.2 Hop 2 — the controller projects a DICT

`projections.capability_bindings` is the **single translation point** between
the CR's list shape and the executor's dict shape
(`k8s/controller/projections.py:81-147`). It emits, keyed by capability id:

```yaml
capabilityBindings:
  observability.list-services:
    provider: mcp
    endpoint_env: CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT   # iff endpointSecretRef
    token_env:    CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN      # iff tokenSecretRef
```

Rules the function enforces, each by raising rather than skipping:

- an entry with no usable `id` or no usable `provider` raises `ValueError` —
  "declared but unbound" is exactly the failure class this seam exists to
  prevent;
- a duplicate capability id raises (one would silently win the dict key);
- two distinct ids that collapse to the same env-var slug raise (they would
  share one `ENDPOINT`/`TOKEN` pair).

`project_deployment` then merges those bindings **per capability id** over the
sentinel directory's own `capabilityBindings`
(`k8s/controller/projections.py:172-228`). Merge semantics, deliberately:

- the CR's binding **replaces** the directory's binding for that id wholesale,
  not key-by-key — a leftover `credential_ref` from a `fixture` binding must
  not survive a rebind to a live provider;
- directory bindings for ids the CR does not mention are **preserved** — this
  is what keeps the fixture path working for capabilities nobody has bound yet;
- the CR's `capabilities` list is **not** copied through as a top-level
  `capabilities:` key. One key, one meaning. A second key carrying the same
  intent is precisely what let the CR's provider choice be silently ignored.

`schemaVersion`, `kind` and `runtime` only ever come from the directory side.
A Sentinel directory with no `deployment.yaml` therefore projects a
schema-invalid file and the executor dies at pod start with a schema error
naming `runtime` — pinned so that failure can never become a silent default.

### 3.3 Hop 3 — the pod injects the env vars

`reconciler._capability_env` walks the same `spec.capabilities` list and emits
`CARDINAL_CAP_<SLUG>_ENDPOINT` / `_TOKEN` env vars sourced from the named
Secrets, keys `endpoint` and `token` respectively
(`k8s/controller/reconciler.py:269-290`).

The predicate is **identical** on both sides: `endpoint_env` appears in the
binding iff `endpointSecretRef` is set, which is iff the pod gets the
`_ENDPOINT` variable. A binding can therefore never name a variable that was
not injected. The slug function (`[^A-Za-z0-9]+` → `_`, strip, upper) is
duplicated as `projections.cap_env_slug` and `reconciler._cap_env_slug` and
the two are pinned byte-identical by
`k8s/controller/tests/test_projections.py::test_cap_env_slug_matches_reconcilers_implementation`
until the de-dupe lands.

The projected `inputs.json` + `deployment.yaml` are mounted from a Secret at
`/config` and the container is invoked as
`sentinel-executor run . --inputs /config/inputs.json --deployment /config/deployment.yaml`
(`k8s/controller/reconciler.py:41`, `:348-365`).

The kopf handler re-projects both files once it has cloned the Sentinel
directory, overwriting the pure reconciler's dir-side-empty version, and
blocks the CR with `CapabilitiesBound=False` /
`reason: MissingCapabilityProvider` if any id in the directory's
`spec.capabilities.required[]` has no CR binding
(`k8s/controller/handlers.py:231-264`, `k8s/controller/capabilities.py`).

### 3.4 Hop 4 — the executor loads it, strictly

`load_deployment` validates against `common/deployment-schema.yaml` and then
applies the checks that are about *usability* rather than shape
(`spike/executor/deployment.py:142-204`): every binding must be a mapping with
a non-empty string `provider`, and any `endpoint_env` / `token_env` present
must be a usable environment-variable name. Failures name the capability id so
an operator knows which CR entry to fix.

The resulting map is a `CapabilityBindings` — a dict subclass that **refuses to
answer "no binding" quietly**. Both `bindings[cap]` and the bare
`bindings.get(cap)` raise `CapabilityNotBoundError`, naming the capability and
listing what *is* bound. An explicit default (`bindings.get(cap, None)`) still
returns it: an escape hatch is fine as long as it has to be typed out.

This is not decoration. Before it existed, a missing binding made
`.get(id)` return `None`, the runtime fell through to the legacy spike
tool-cache path, and a Sentinel CR that carefully declared `provider: mcp` ran
against hand-populated JSON files with no error anywhere.

### 3.5 Hop 5 — the node resolves a provider

For a `tool` node, `runtime_serve._run_node` reads `config.toolRef`, looks the
binding up by that exact string, reads `binding["provider"]`, and calls
`capabilities.resolve_provider(tool_ref, provider_id)`
(`spike/executor/runtime_serve.py:272-289`).

**The capability id is the join key throughout.** `spec.capabilities[].id` in
the CR, the `capabilityBindings` map key, and a tool node's `config.toolRef`
are the same string. Nothing translates between them.

### 3.6 What is pinned in CI

`spike/executor/tests/test_cr_deployment_roundtrip.py` runs a realistic
f89df52b-shaped CR through `project_deployment` → `load_deployment` and
asserts: the CR's provider beats the directory's `fixture` default; every
`endpoint_env`/`token_env` a binding names is actually in the pod spec's env
list; a capability with only `endpointSecretRef` gets neither the token binding
nor the token env; ids the CR omits keep their fixture binding; an unbound
capability raises `CapabilityNotBoundError`; no dir side ⇒ schema-invalid with
`runtime` in the message; and the projected YAML is byte-stable across calls
(the Secret must not churn on every reconcile).

If either side of the seam changes shape, that file fails with a message that
says which side.

## 4. The provider registry

Multiple providers may satisfy one abstract capability. The registry is keyed
by the pair (`spike/executor/capabilities.py:216-268`):

```python
@provider("observability.list-services", "mcp")
def _list_services_via_mcp(node_id, args, ctx): ...
```

- **Registration is import-time** and duplicate registration for the same
  (capability, provider) pair raises `RuntimeError`. One file per provider.
- **Lookup failure is loud**: `resolve_provider` raises `UnknownProviderError`
  listing the providers actually registered for that capability.
- **Signature**: `(node_id: str, args: dict, ctx: dict) -> Any`.
- **`ctx`** is built by `runtime_serve._run_node` and carries exactly four
  keys: `run_dir`, `sentinel_dir`, `capability_id`, and `binding` — the
  binding dict from `deployment.yaml`, which is where a provider finds its
  `endpoint_env` / `token_env` / `credential_ref`. A provider reads the
  *values* from `os.environ` at call time; the binding only ever names
  variables.

`CAPABILITY_BINDINGS` / `resolve()` higher up the same module is the
**spike-era, single-provider** lookup used by the `execute` subcommand and the
tool-cache protocol. It is not part of this contract and is not reachable from
a `deployment.yaml`-driven run (see §7).

### 4.1 The fixture provider

`fixture` is registered for all six known capability ids
(`spike/executor/capabilities.py:315-325`). It reads, in order:

1. `<sentinel-dir>/fixtures/<node-id>.json` (per-node override)
2. `<sentinel-dir>/fixtures/<capability-id>.json`
3. `<sentinel-dir>/fixtures/<capability_id_with_underscores>.json`

and returns the first that exists, verbatim. A file may instead hold
`{"_byArgs": {<canonical-json-args>: result}, "_default": result}`; a miss with
no `_default` raises. No file at all raises `FileNotFoundError` naming the
directory and the three names it looked for.

`fixture` is the default for tests and is universally admitted by the registry.
Remote-mode lint **fails** a `fixture` binding unless
`execution.allowFixtures: true` is set, so fixtures cannot reach production by
accident (`spike/executor/lint_remote.py:344-357`).

### 4.2 The `mcp` provider

`spike/executor/providers/mcp.py` registers `PROVIDER_ID = "mcp"` for the four
observability capabilities, mapping each to an aggregator-namespaced gateway
tool via `CAPABILITY_TOOLS`:

| capability id | gateway tool |
|---|---|
| `observability.list-services` | `lakerunner__list_services` |
| `observability.error-overview` | `lakerunner__error_overview` |
| `observability.query-logs` | `lakerunner__execute_logs_query` |
| `observability.query-metrics` | `lakerunner__execute_metrics_query` |

It is the first provider to actually consume the binding contract in §3, and
what it consumes is worth stating exactly:

- **Env-var names come from the binding and are never recomputed.** The
  provider reads `binding["endpoint_env"]` / `binding["token_env"]`, then reads
  those variables' values from the process env. The binding is authoritative
  because it only ever names variables the controller actually injected (§3.3).
- **A missing `endpoint_env` / `token_env` is a hard error**, not a fallback to
  a default endpoint. The message names the CR field the operator has to add.
- **`credential_ref` (`env://VAR`) remains the local/dev path** for the token.
- **Two optional binding keys** beyond the schema's named ones (the schema's
  `additionalProperties: true` admits them): `params` — argument defaults
  merged *under* the node's rendered arguments, including `org_id` which is
  consumed by URL construction rather than passed as a tool argument — and
  `timeoutSeconds`.
- **`instance` is required on every lakerunner tool.** The provider checks for
  it before the round trip and fails with `McpMissingInstanceError` naming both
  ways to supply it (`arguments.instance: "${inputs.instance}"` on the node, or
  `params.instance` on the binding).

Providers are auto-imported: `capabilities._import_providers()` runs
`providers.import_all()` at the bottom of `capabilities.py`, so importing
`capabilities` populates the registry. That loop deliberately does **not**
swallow `ImportError` — a silently-missing capability provider is the exact
failure mode this seam exists to remove.

## 5. What lint enforces (R10)

`sentinel-lint` remote mode cross-checks the sidecar against the artifact and
the registry (`spike/executor/lint_remote.py:292-389`):

- every non-`llm.*` id in `spec.capabilities.required[]` has a
  `capabilityBindings` entry;
- every `capabilityBindings` entry maps to a declared required id (no orphans);
- `provider: fixture` requires `execution.allowFixtures: true`;
- any other provider must appear in
  `common/capabilities-registry.yaml → capabilities.<id>.providers[]`.

`llm.*` capabilities are bound through `llmBindings` and are covered by R13,
not R10.

## 6. The v1 overlay-bindings story (why `kind: Variation` is refused)

`lint.py` refuses `kind: Variation` with `STRUCT-VARIATION` and points here.
The reason is a genuine unresolved design question, not missing typing.

`sentinels.md` §21–23 gives a Variation two things that overlap this document:
a `spec.bindings` block, and a `replace-binding` patch op applied at step 5 of
the resolution order. A deployment site therefore has **two** places that can
change what a tool node talks to — the Variation overlay (authored, travels
with the reuse claim, digest-affecting) and `deployment.yaml` (site-local, not
part of any digest).

v1 must answer, before any of it is implemented:

1. **Precedence.** Does a Variation's `replace-binding` outrank the site's
   `capabilityBindings`, or the reverse? Today the analogous question for CR vs
   directory is answered — CR wins, wholesale, per id (§3.2) — and the same
   answer is the obvious candidate, but it is not yet written into a schema.
2. **Digest scope.** Bindings that ride the Variation affect the resolved-graph
   digest; bindings that ride `deployment.yaml` do not. Which class each op
   lands in determines whether two sites running "the same" Variation are
   comparable as reuse evidence.
3. **Where the credential indirection lives.** `deployment.yaml` never inlines
   a secret. A Variation authored for sharing must not become the place someone
   pastes an endpoint.
4. **Safe-mode scope.** §23 restricts a safe Variation to declared variation
   points. Whether a binding replacement counts as a variation point, or is
   always an unsafe fork, is undecided.

Until those are answered, `kind: Variation` is refused at lint rather than
resolved with a guessed precedence. Nothing in `spike/executor/` resolves
Variations today: `runtime_serve` loads only `sentinel.yaml` from the Sentinel
directory and `executor.py` hard-codes `variationDigest: ""`
(`spike/executor/runtime_serve.py:116`, `spike/executor/executor.py:589`).

## 7. Known divergences (as of this writing)

These are real and checkable. They are listed here rather than papered over.

1. **The legacy tool-cache fallback in `runtime_serve` is now unreachable.**
   `spike/executor/runtime_serve.py:275-280` does
   `binding = deployment.capability_bindings.get(tool_ref)` and falls back to
   `capabilities.resolve(tool_ref)` when the result is `None`. Since the seam
   fix, that `.get()` with no default *raises* `CapabilityNotBoundError`
   instead of returning `None`, so the `if binding is None` branch is dead for
   any `Deployment` produced by `load_deployment`. The behaviour is the
   intended one (loud failure); the dead branch should be deleted so nobody
   reads it as a live path.
2. ~~**`mcp` is registered in the executor but not admitted by the registry.**~~
   **RESOLVED in this change.** The open question was whether the registry
   should gain `mcp`, or whether the CR-facing name should stay the
   transport-agnostic `lakerunner` with `mcp` as an implementation detail.
   Settled in favour of the former: `common/capabilities-registry.yaml` now
   lists `providers: [lakerunner, mcp]` for `observability.list-services`,
   `.query-logs` and `.error-overview`, and `[lakerunner, prometheus, mcp]`
   for `.query-metrics`. The registry is a pure allow-list membership check —
   nothing selects a provider *from* it — so admitting `mcp` alongside
   `lakerunner` costs nothing and keeps the binding's `provider` field the
   single place a provider is chosen. A binding with `provider: mcp` now
   passes lint R10; verified by a differential lint run.
3. **The CR's `spec.sinks` is a second silent drop, structurally identical to
   the one §3 just fixed.** `projections.project_deployment` writes the CR's
   sinks list to a top-level `sinks:` key
   (`k8s/controller/projections.py:216-217`), but the executor routes findings
   from `findingsRouting` only (`spike/executor/runtime_serve.py:328`,
   `deployment.py`'s `findings_routing`). Nothing reads `sinks:`. An operator
   who sets `spec.sinks` on the CR gets no error and no effect: delivery is
   decided entirely by the Sentinel directory's `findingsRouting`. The
   capability seam and the sink seam want the same treatment — one key, one
   meaning, projected into the shape the executor actually reads.
4. **`capabilities.py:227`'s comment under-describes `ctx`.** It names
   `run_dir`, `sentinel_dir`, and "provider-specific state"; the real ctx also
   carries `capability_id` and `binding`, both of which providers depend on.
5. **The pure reconciler reports `CapabilitiesBound=True` unconditionally**
   (`k8s/controller/reconciler.py:152`) with the message "capability validation
   deferred to handler with dir-side yaml". The real check does run, in
   `handlers.py`, and does block — but a reader of the pure layer alone would
   be misled, and any consumer of that condition sees `True` before the check
   has happened.

## 8. Phase 2 (named, not built)

- Secret schemes beyond `env://`: `k8s-secret://`, `vault://`, `aws-sm://`
  (`spike/executor/secrets.py` refuses these by name today).
- Sinks beyond `stdout` — a Slack sink can ride the same gateway client and
  auth path as the MCP capability provider.
- Cross-run dedupe keyed `sentinel + variation + dedupeKey`. The digest is
  already computed (`spike/executor/executor.py:847-848`); nothing persists or
  consults it across runs.
- In-cluster llm nodes: model key + token-budget enforcement behind
  `llmBindings`.
- Variation overlay bindings (§6).

---

*Companion documents:* `docs/specs/sentinel-crd.md` (CRD + runtime shape),
`sentinels.md` (the Sentinel/Variation spec), `sentinel-lint-plan v0.3` (the
lint rule catalogue; `spike/executor/lint.py:4` points at
`docs/sentinel-lint-plan.md`, which is likewise not yet written).
