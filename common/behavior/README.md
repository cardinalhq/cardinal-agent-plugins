# Behavioral Programs in the Cardinal Claude plugin

The installed plugin registers `cardinal-behavior` alongside the existing Cardinal
MCP connection. It derives the authenticated organization route from
`CARDINAL_MCP_URL` and uses the existing `CARDINAL_MCP_API_KEY`. Maestro resolves
the organization's LakeRunner integration and forwards with its server-side
data-plane credential. The plugin never receives that credential. Its scoped MCP
key is not a Query API key and is never sent to a guessed Query API host. Requests
reject redirects so custom authentication headers cannot follow another host.
No model credentials enter the plugin or UDF.
The deployed runtime injects the authorized JEV backend.

The normal Claude workflow is:

1. `get_behavior_sdk`: fetch the deployed SDK artifact and read its real Python
   sources, profiles, CompilePlan schema, and executable examples. LakeRunner
   imports this exact immutable release artifact from the public
   [cardinalhq/behavior-sdk](https://github.com/cardinalhq/behavior-sdk) repository.
   The SDK version, source commit, public artifact URL, and SHA-256 appear in the
   response and are retained with compilation and acceptance. The
   plugin verifies its SHA-256 content address and caches the immutable artifact;
   it contains no independently maintained SDK declarations. The response starts
   with `sdk_directory` and `sdk_files`: exact verified public files materialized
   under the digest cache, so Claude can page through real Python source using
   its normal Read tool even when the complete MCP response is large.
2. Author a candidate from the user's contract, then call `compile_behavior`
   with `description`, `service_name`, `udf_source`, and `compile_plan`. The plugin
   pins the observed SDK, profile, and host runtime identities. The deployed
   compiler checks syntax, imports, public API, and mechanical requirements.
   Up to eight authored `teaching_examples` are optional compiler checks.
3. Call `test_behavior` with the compiled `diagnostic_version`, real `trace_ids`,
   their `service_name`, and an RFC3339 `start`/`end` window of at most one hour.
   Supply `expected_verdicts`, a map from every teaching trace ID to its expected
   MATCH, NON_MATCH, or UNKNOWN verdict. Execution failures return ERROR. The deployed API reads those
   traces through the production catalog and runs the candidate with the same
   production SDK, sandbox, and authorized JEV backend used for population runs.
   Results include actual verdicts, witnesses, Recorder output, and JEV receipts.
   No local evaluator or synthetic replacement trace is used by this tool.
4. Inspect the contract, source, compile receipt, and teaching results. Revise
   and compile again if necessary. The optional `udf_source` on `test_behavior`
   must exactly match the compiled candidate. For example, discover behavior in
   trace A, choose contrasting trace B, and confirm A MATCH/B NON_MATCH before
   acceptance. Teaching examples establish intent, not population accuracy.
   Every test batch is retained in the compilation scope identified by the exact
   description and service name, including failed batches. A repair with the same
   description/service inherits that entire suite. Use `get_behavior_compilation`
   to inspect coverage and `run_behavior_regressions` to replay it in batches of
   at most eight real traces. The replay continues after an ERROR or failed batch.
   A new replay invalidates previous observations for that candidate, preventing
   an interrupted run from silently using earlier passing results.
   Ordinary test batches also reserve their cases before contacting the backend.
   Failed requests and invalid receipts leave missing coverage; an older response
   cannot replace a newer test attempt. Receipt and acceptance updates are
   serialized so concurrent plugin processes preserve the incumbent metadata.
5. `accept_behavior` explicitly accepts the inspected immutable DiagnosticVersion.
   Acceptance requires a real teaching receipt with expected verdicts and binds
   its trace IDs, expected/actual results, compile/JEV receipts, and SDK/profile/
   runtime identities. Identity disagreements fail closed.
   The plugin also verifies every accumulated regression case for that version.
   A latest passing batch cannot hide an older failure or missing case. The
   previously accepted incumbent remains selected on compilation, test, or
   acceptance failure. This development gate does not claim independent
   qualification; the separate blind-audit lifecycle is described below.
6. `execute_behavior` submits the accepted version, `population` (exact service
   name), and RFC3339 `start` and `end` to the authenticated deployed API.
7. Call `next_behavior_result` until a completed receipt appears. Findings expose
   trace ID, verdict, bounded reason, witness references and coverage gaps. A
   running count is progress, not the final population denominator.
8. `get_behavior_execution` inspects durable shard references and bounded JEV
   metadata for a selected trace without advancing the result cursor.
9. `render_storyboard` fetches committed detailed receipts and renders expandable
   evidence from a completed execution. Raw model inputs stay in private host
   artifacts and the Storyboard, outside the tool's compact findings.

SDK artifacts, compiled versions, teaching results, acceptances, and execution
receipts are retained under `~/.cardinal/behavior-executions` by default.
`CARDINAL_BEHAVIOR_OUTPUT_DIR` can select another private directory. The bridge
checks SDK artifact and source hashes, SDK/profile/runtime identities, result
version/UDF/adapter identities, duplicate trace IDs, and completed counts.
Multiple compiled versions can be tested, executed, and inspected after a plugin
restart. Read `get_behavior_sdk` in each authoring session before compiling.

## Canonical compilation components

All reusable authoring and qualification code lives in this plugin's
[`compilation/`](compilation/) package. The deployed SDK compiler and JEV remain
external pinned runtime dependencies. `server.py` supplies MCP transport and
production execution; it inherits the authoring bridge from this package.

| Component | Responsibility |
| --- | --- |
| `hosted.py` | SDK discovery, deployed compile/test/accept, complete-suite replay |
| `regression.py` | Persistent development cases, receipt checks, accepted incumbent |
| `workflow.py` | Explicit offline compile → challenge → diagnose → repair → qualify → freeze cycle |
| `diagnosis.py` | Independent raw-trace adjudication, cited diagnosis, integrity validation and intervention routing |
| `engine.py` | Bounded repair, provisional promotion, complete regressions, one hidden audit, freeze and amortized cost |
| `corpus.py` | Source-shaped corpus rendering and independent blind fidelity/gold review |
| `llm.py` | Optional stateless offline model transport and usage receipts |
| `sdk_adapter.py` | Configurable binding to the unchanged SDK compiler, sandbox and embedded JEV |
| `identity.py`, `lifecycle.py` | Input fingerprints, explicit requalification, optional evidence collection |
| `runtime.py` | Frozen-bundle integrity/admission and one unchanged SDK invocation |
| `storage.py`, `files.py` | Immutable execution reservations and private receipt persistence |

The release build includes this entire package with the existing behavior MCP
launcher. No component imports an experiment checkout or assumes a developer's
workspace layout. Historical `behavioral-program-qualification` and
`behavioral-compilation-correctness` artifacts remain sealed research records;
future implementation changes belong here. `compilation/provenance.json`
records the historical migrated source and instruction hashes. Discovery/review
instructions and production runtime methods remain unchanged; diagnosis and targeted
repair prompting intentionally supersede the historical repair instruction. This source consolidation does not itself publish or install a release.

For a new offline qualification cycle, import
`compilation.workflow.compile_and_qualify` with `common/behavior` (or the installed
`lib/behavior`) on the Python path. Supply the source/plan, contract/context,
exact SDK documentation, independent role transport, reviewed corpus provider,
pinned SDK adapter, lifecycle fingerprints, dependency files, and an explicit
`Policy`. The SDK adapter receives the existing profile, JEV factory, sandbox,
evidence auditor and identity verifier; it never defines replacement SDK APIs.
The provided corpus renderer supports the original four-span native episode
shape. Supply its native/source schema validators explicitly, or inject a
reviewed corpus provider for another trace shape. No permissive schema fallback
is provided. Corpus discovery/review instructions and the behavioral contract are unchanged.

Before any repair, `Diagnoser` uses a fresh raw-trace-only review with no source,
prediction, prior rationale or labels, then a separate execution diagnosis context.
The persisted diagnosis includes expected/actual verdicts, candidate/entity/time
scope, the earliest unsupported inference, exact JSON-pointer citations and quotes,
actual JEV propositions/packets/answers, mechanical failures, and a permitted intervention.
The controller validates source/trace/execution/contract hashes, literal citations,
gold consistency and receipt examination before invoking the repair callback.
These checks prove artifact integrity, not semantic correctness of the diagnosis.

Candidate selection, binding, temporal logic, coverage and composition allow a
small Python repair; a defective JEV proposition allows proposition repair; invalid
SDK usage allows SDK-compatible repair. An incorrect answer to an adequate JEV
proposition receives independent semantic re-evaluation and no automatic code
rewrite. Ambiguous gold quarantines the fixture; insufficient evidence or explicit
`NO_CODE_REPAIR_JUSTIFIED` stops the cycle with the incumbent preserved. No reviewed
counterexample subsequently counts as independent audit evidence.

Direct `Qualifier` and `lifecycle.requalify` callers must inject `diagnose` if repairs
are needed; omission fails closed before a repair request. Compile failures consume
the same bounded budget. A rejected runtime candidate stays separate feedback;
subsequent diagnoses concern the incumbent. Each repair request includes one justified
counterexample, its diagnosis, relevant source, rejection feedback and exact SDK
interfaces, while the promotion gate checks every accumulated development case.
Promotion requires execution/provenance validity, no lost correct cases, and at least
one improved case; a no-op cannot replace the incumbent.

`compile_and_qualify` returns `QUALIFIED` only after every development case passes
and a previously untouched, independently reviewed audit passes for MATCH,
NON_MATCH and UNKNOWN. Failed audits are terminal for that cycle. Rejected
repairs leave the incumbent unchanged. Successful cycles export an immutable
bundle; incomplete or failed cycles retain their evidence and return
`CANNOT_QUALIFY`. The frozen bundle is not automatically accepted or deployed by
the hosted API. An application supplies its existing trusted admission mechanism.

Qualification costs include authorship, review, diagnosis, challenge, repair, compilation
and SDK test executions. Supply their actual usage rows; unavailable prices or
usage remain unknown. `cost_summary` keeps them separate from steady-state SDK/JEV
execution and amortizes qualification cost as `Q / expected_lifetime_executions`.
The untouched held-out experiment remains a separate evaluation of frozen
programs. Passing teaching or qualification fixtures is not a held-out improvement
claim. No downstream adjudicator corrects production verdicts.

`lifecycle.requalify` starts an explicit new offline cycle with parent lineage
after program, agent, telemetry, contract, SDK or JEV identity changes. It never
mutates the old bundle. Optional `collect_sample` only queues existing evidence
for a future cycle; it performs no inference and is not required for execution.

Hosted development history is scoped to the plugin's private output directory.
Imported older candidates must be recompiled through `compile_behavior` before
new acceptance so that their regression scope is registered. Existing accepted
versions continue executing unchanged. The deployed API still validates its
own acceptance receipt; clients bypassing this plugin do not gain its accumulated
suite guard. Changing the relevant contract starts a distinct development scope;
independent requalification must explicitly record the change and parent lineage.

For explicit deployments, `CARDINAL_BEHAVIOR_CONFIG` or `--config` can name a JSON
configuration; see `config.example.json` for an operator's direct Query API
configuration. `CARDINAL_BEHAVIOR_API_URL` also remains an explicit direct URL
override and reads its matching data-plane credential from `CARDINAL_QUERY_API_KEY`.
It never falls back to the scoped MCP key. Direct JSON configuration can
select its credential with `api_key_env`; it must not reuse a scoped Maestro MCP
key. `headers_file`, if used, must be private
(mode 0600) and contain only supported API authentication headers. Authenticated
normal API calls derive the organization from the existing Cardinal key.

The release build copies canonical `common/behavior` into `lib/behavior`; the
launcher supports both installed plugins and source checkouts. Legacy explicitly
configured `diagnostic_version`/`artifacts` selection remains available for old
receipts, but the normal workflow does not load frozen default artifacts or
require manual registration.

Run focused checks with:

```sh
python3 -m unittest discover -s common/behavior/tests -v
python3 -m unittest discover -s tests -p test_behavior_release.py -v
```

The optional `test_retained_failures.py` replay requires
`CARDINAL_BEHAVIOR_RETAINED_FIXTURES` to point to the sealed qualification workspace.
It executes the three retained failing programs against their complete retained
development suites using this package's SDK adapter and controller, forbids model
backend calls, and uses an explicit deterministic incumbent stand-in. It is a
mechanical regression test, not another qualification experiment.

## Question-first lifecycle and qualified execution

`compilation.autonomous.AutonomousLifecycle.start(agent_id, behavioral_question,
time_range=None)` generates the contract, Python and CompilePlan itself. The
operator registers authoritative instructions, implementation/tool definitions,
telemetry schema and an authenticated trace collector once per agent. Question
callers do not supply program artifacts or semantic labels. Missing authoritative
source returns `INSUFFICIENT_SOURCE`; missing evidence within an execution is a
contract `UNKNOWN` condition. Discovery does not calculate population prevalence.

The small `application.build(config)` binding uses authenticated immutable exports
and the exact SDK/runtime configured by the operator. The `RetainedTelemetry`
collector can be replaced by the application's existing catalog/query collector;
exports record origin receipts, exact source hashes, selection, exclusions and
separate discovery/population membership. Reusing an export only evaluates that
export: continuous fresh collection requires connecting that provider to the
existing host worker. This change does not install a scheduler or deploy routes.

A concrete invocation from the repository root is:

```sh
PYTHONPATH=common/behavior python3 -m compilation.cli --config /private/behavior-host.json \
  create --agent-id megan \
  --question 'Across this agent’s executions, how often does it make a recommendation or take an action without sufficient supporting evidence?'
```

The configuration contains `output_dir`, a pinned `runtime` directory,
`agents`, `principal`, `model`, and `policy` (defaults: two repairs, four audit
cases per label, 10,000 lifetime executions for amortization). Each agent entry
contains `revision`, `collection`, four source-path maps named `instructions`,
`implementation`, `tools`, `telemetry_schema`, plus `telemetry.manifest` and its
`sha256`. The export manifest supplies `synthetic: false`, `time_range`,
`source_receipts`, `exclusions`, and `splits.discovery` / `splits.population`
entries with `file`, `sha256`, `trace_id`. Runtime `manifest.json` lists file
hashes and the canonical SDK/profile/host identities; the directory contains the
unchanged installed host/SDK and an operator-selected isolation runner. No SDK
source or runtime semantic replacement is shipped by this package.

The source-linked compiler, retained independent `Diagnoser`, existing `Qualifier`
and unchanged embedded JEV run the full cycle. Repair promotion requires valid
compilation, zero errors, valid provenance, no correct-case regressions, and at
least one correction across all accumulated development cases. Rejected repairs
consume budget and preserve the incumbent. The hidden audit is generated only
after selection and must meet the unchanged gate before freezing. SDK versions
are not interchangeable: prompts use the supplied SDK's actual CompilePlan
schema (0.1.1 uses version 1); compile, qualify, freeze and execution use one pin.

`DEVELOPMENT_ACCEPTED` is the existing teaching/regression acceptance result.
It is never a qualification. `QualifiedRegistry.issue` is an internal host-worker
operation that rechecks hidden audit evidence and frozen hashes, then authenticates
the admission record using a private persisted HMAC key. The caller supplies only
an opaque qualified ID. Admission binds source, CompilePlan, generated contract,
agent sources, schema, SDK, JEV, policy and the evidence manifest. Drift fails
closed. There is no certificate-upload route. Each tenant must receive its own
host instance, storage and authenticated principal binding. Keep the registry,
issuer key and evidence artifacts outside client access.

After a `QUALIFIED` result, use the returned ID:

```sh
PYTHONPATH=common/behavior python3 -m compilation.cli --config /private/behavior-host.json \
  execute --qualified-id "$QUALIFIED_ID"
```

`inspect`, `disable`, `revoke`, `rollback --target-id ...`, and
`receipt --execution-id ...` provide the minimum controls. Rollback only selects
an older qualification for the same agent/question whose current identities
still match; revoked qualifications cannot return. Execution is serialized;
controls apply between population batches. Signed completed receipts retain
eligible execution counts, all four outcomes, exclusions, raw trace hashes and
SDK evidence references. UNKNOWN and ERROR never count as NON_MATCH.

`QualifiedHost.dispatch` is the embedding boundary for an existing authenticated
application router. `qualified_api.handler` supplies a small HTTP adapter at
`/api/v1/qualified-behaviors/{create,execute,job,inspect,receipt,disable,revoke,rollback}`.
The local `serve` command binds loopback and obtains its credential from the
configured `http_token_env`; the adapter accepts bearer or the plugin's existing
`x-cardinalhq-api-key` header. Create/execute persist a job before responding
`PENDING`; poll `job` rather than repeating creation. Interrupted jobs are retained
and do not silently retry or reset repair budgets. Production deployment must
wire the routes to the host's tenant authentication, worker supervision and fresh
telemetry provider; this repository change alone does not make them available on
the existing deployed Query API.

The corresponding MCP tools are `create_qualified_behavior`,
`execute_qualified_behavior`, `get_behavior_lifecycle`,
`inspect_qualified_behavior`, `get_qualified_behavior_receipt`, and
`control_qualified_behavior`. They fail if qualified routes are unavailable;
there is no fallback to development acceptance. Production execution calls the
frozen SDK program with embedded JEV only. Corpus authoring, independent reviews,
diagnosis and repair are offline operations and never downstream adjudicators.

For recovery after an integration failure, an operator may configure
`model_reuse_from` to its private retained model-call directory. Only an exact
request/model match can reuse discovery, contract, program or corpus authorship;
the original response is checked against its raw model events and a linked
zero-invocation receipt is written. Independent reviews, diagnosis, repairs and
baselines always run in fresh contexts. Qualification evidence is never reused
through this option, and a failed audit is never converted into admission.

`discovery_reuse_job` can pair that cache with a retained discovery snapshot after
an integration failure before development begins. The host freshly verifies the
same request, policy, source, telemetry and SDK/JEV identities. Only local export
read-latency metadata may differ; its new measurement is retained separately.
Recovery refuses any prior job whose development/qualification directory exists,
so this mechanism cannot reset repair budgets or rerun a failed hidden audit.
