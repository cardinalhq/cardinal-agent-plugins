# `/cardinal:install-site` — design

**Status:** proposed
**Repo:** `cardinal-agent-plugins`
**Depends on:** the user-scoped API token (`conductor`
`docs/superpowers/specs/2026-07-17-user-scoped-api-token-design.md`) — hard
dependency, nothing here works without it
**Reads better with:** perch-reported namespace (`conductor`
`docs/superpowers/specs/2026-07-17-perch-reported-namespace-design.md`)

## What this is

A customer-facing skill that stands up a Cardinal site: creates it in Maestro,
installs perch into the user's Kubernetes cluster, optionally adds a POC
Lakerunner, and verifies both came up.

Audience is **customers self-serving**, not the team. That sets the bar: every
error path is product surface, and "it 409'd" is not a recovery story.

## Key finding: no new Maestro endpoints are needed

The entire flow already exists as REST. Auth is `Authorization: Bearer <token>`
plus an `X-Org-Id` header — there is no session cookie anywhere in Maestro, so
nothing here is UI-coupled. The only missing piece was a principal the agent can
present, which is the dependency above.

| Step | Call |
|---|---|
| 1–2. verify owner, pick org | `GET /api/me` → `orgs[]` with `role` (`me.ts:94-105`) |
| 3. list sites, name the new one | `GET /api/orgs/:orgId/sites` (`sites.ts:221`) |
| 4. pick cluster | local: `kubectl config get-contexts` |
| 5. pick namespace | local: `kubectl get ns` |
| 6. create site | `POST /api/orgs/:orgId/sites` → `201 { site, apiKey: { plaintext, prefix } }` (`sites.ts:165`) |
| 6. install perch | `helm install` (see below) |
| 7. verify perch | `GET .../sites/:siteId/bootstrap-status` until `phonedHome` (`sites.ts:459`) |
| 8. POC lakerunner | `POST .../workloads/license/resolve` then `POST .../workloads/lakerunner` (`site-workloads.ts:312, 199`) |
| 9. verify + connect info | `GET .../workloads` and `GET .../workloads/maestro/credentials` (`site-workloads.ts:179, 491`) |

The site row and its `psk_` key are minted in **one transaction**
(`sites.ts:171-211`), which is exactly what step 6 needs — there is no window
where a site exists without a key.

## Cluster boundary

The agent runs `helm` itself, behind a **dry-run and explicit confirm gate**. It
renders the exact command, shows it, and waits for approval before executing.
Nothing touches the cluster without the user seeing what will run.

This is the only step that mutates the cluster. Everything else — including all
verification — is Maestro-side, because perch pulls its config from Maestro and
pushes status back. The agent never needs cluster read access to answer "did it
work".

## The helm install

The perch chart already has a Maestro-managed mode (`charts/perch/values.yaml`),
mutually exclusive with static-config mode:

```sh
helm install perch oci://public.ecr.aws/cardinalhq.io/perch \
  -n <namespace> --create-namespace \
  --set site.id=<siteId> \
  --set site.apiKey=<psk_...> \
  --set site.endpoint=<maestro base url>
```

`site.apiKey` is rendered into a chart-owned Secret and injected by reference,
never as a literal env. The skill must not echo the plaintext into the
transcript — show the command with the key redacted, pass the real value via
`--set` at execution.

## Namespaces

There are two, and conflating them is the trap.

- `helm -n <ns>` — where the **operator** lives. Perch's RBAC is scoped to it,
  and it is the only namespace perch can reconcile.
- `maestro_sites.workload_namespace` — Maestro's record of where the workloads
  are. Perch overrides its config copy with `POD_NAMESPACE` regardless.

**Ask once; pass the same value to both** — `workloadNamespace` on `POST /sites`
and `-n` on helm. They agree by construction, perch's override is a no-op, and
`/maestro/credentials` returns port-forward instructions that work.

Suggest `cardinal`; verify it is free with `kubectl get ns` and re-prompt if
taken. Once project A lands, Maestro learns the real namespace from perch's
first heartbeat and this stays correct even if the user overrides `-n` by hand.

## Two failure modes that must be handled, not discovered

**`mode` must be `"operator_managed"` on create.** The field is optional in the
POST body (`sites.ts:93-101`), but `POST /workloads/lakerunner` 409s
`site_not_operator_managed` (`site-workloads.ts:218`) — at step 8, long after the
site exists and perch is installed. Always send it explicitly.

**`409 operator_not_phoned_home`** (`site-workloads.ts:221`) means step 8 ran
before perch checked in. Step 7 is a hard gate, not a progress indicator.

## Recovery

The skill creates a site before it installs anything. A helm failure therefore
strands a real site row, and the flow must be resumable rather than leaving
orphans:

- **`psk_` plaintext is recoverable until first phone-home.**
  `GET .../sites/:siteId/bootstrap-step` re-serves it (`sites.ts:476`); it is
  cleared once perch reports (`perch-sync.ts:123`). So a re-run before phone-home
  can re-fetch the key and retry the install.
- **After phone-home**, rotate: `POST .../sites/:siteId/bootstrap-key`
  (`sites.ts:489`, 409s on `site_revoked` / `already_registered`).
- **Re-running with an existing name** hits `UNIQUE (org_id, name)`. Detect the
  existing site at step 3 and offer to resume it rather than failing on create.
- **Abandoning** is `DELETE /api/orgs/:orgId/sites/:siteId` (`sites.ts:425`).
  Offer it; do not do it silently.

`PATCH` only accepts `name` (`sites.ts:106-108`) and `endpoint` is explicitly
immutable (`400 endpoint_immutable_v1`, `sites.ts:401`) — a wrong endpoint means
delete and recreate. Get it right on create.

## Finish

Print the site-detail UI URL and let the user take it from there — but print the
connect block too, rather than making them go hunting:

- login email, `baseUrl`, `service`, `namespace`, `port` — all from
  `GET .../workloads/maestro/credentials` (`site-workloads.ts:513-521`)
- the port-forward: `kubectl port-forward -n <ns> svc/<service> <port>:<port>`
- how to read the password:
  `kubectl get secret <passwordSecret> -n <ns> -o jsonpath='{.data.<passwordKey>}' | base64 -d`

Maestro never receives that password — perch mints it inside the customer's
cluster (`site-workloads.ts:511-512`). The skill can only say where it lives,
and should say so plainly rather than implying it could fetch it.

`/maestro/credentials` returns `409 { reason: "not_ready" }` before bootstrap and
`409 { reason: "no_owner" }` if the org has no owner — the latter is a standing
misconfiguration that never resolves on its own (`site-workloads.ts:505-508`),
so report it as such rather than retrying.

## Adapters

Claude first. Port to codex/gemini once the flow is proven, following the
`optimize-toolkit` pattern (#18 → #19 → #22 → #23). Cursor has no `connect`
skill and is out of scope.

The skill is a script in `adapters/claude/bin/` alongside `cardinal-connect`,
not prompt-driven HTTP. It reads the token from the `0600` file via
`core/cardinal_core`, so the sequencing, the 409 handling, and the retry logic
are testable without a model in the loop. The `SKILL.md` invokes it and handles
the conversation — the same split `cardinal-status` already uses.

## Testing

- **Unit:** each step's request shape and its documented failure responses,
  against a fake Maestro. Specifically: `mode` is always sent as
  `operator_managed`; the two 409s produce actionable messages; a name collision
  offers resume.
- **Unit:** the token is never written to the transcript; the `psk_` never
  appears in a rendered command.
- **Integration:** the full flow against a dev Maestro and a throwaway cluster
  (see the spike, below).

## Plan notes

**First task is a manual spike, before any code.** Drive all nine steps by hand
against dev with a token copied from the SPA's devtools, pointed at a throwaway
cluster. Record: how long `bootstrap-status` takes to flip to `phonedHome` (this
sets the poll interval and timeout), what `license/resolve` does when the org has
no license, and the exact bodies of both 409s.

Every fact that shaped this design came from reading code, and several inverted
the design when found — the chart's `site` mode, `drop_perch_installation_key`,
a namespace column that lies, and perch already reporting the namespace Maestro
discards. The 409 paths are the same shape: cheap to learn in a spike, expensive
to learn in a customer's cluster after the site row already exists.

## Success criteria

- A customer with owner role installs a working site and POC Lakerunner without
  reading docs or leaving the agent.
- An org **member** is refused — by the token's principal hitting the existing
  `requireOrgRole("owner")` gate, with no check written in the skill.
- Nothing touches the cluster without an explicit confirm.
- Every failure leaves either a working site or a clearly stated way forward.
