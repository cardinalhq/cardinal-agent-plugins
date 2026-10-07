# Deployed behavior product loop

This opt-in Cardinal Claude MCP bridge uses a configured accepted DiagnosticVersion.
It submits to the deployed API and observes committed
findings. It imports no evaluator, trace adapter, compiler runtime, or local fixture.
Claude selects the existing accepted contract; this preview does not compile new
behaviors. The default is the investigator version from LakeRunner PR #1892:
MATCH means a readiness statement occurred before `submit_report`, not
that the investigator's evidence was actually insufficient.

Create a private runtime configuration from `config.example.json`. `headers_file`
must have mode 0600 and contain the existing API authentication headers. Alternatively
set `api_key_env` to an environment variable containing the Cardinal API key (default
`CARDINAL_MCP_API_KEY`), or `internal_key_env` for an internal key. Keys are never
returned to the model. The private output directory retains execution receipts and
the expandable evidence Storyboard. Keep it outside the source repository.

For another compiled and accepted diagnostic registered with the deployed backend,
set `diagnostic_version`, `population` (the exact service name), `description`, and
`artifacts` (a directory containing the accepted `versions/` and `previews/`).
Set `adapter_sha256` when it differs from the profile digest. An optional
`compile_receipt_ref` identifies the prior compilation receipt in the selection
response. Compilation and acceptance happen before registration; this bridge
selects and executes the resulting immutable version.

The Megan trial uses this configuration to check whether an analytics answer offers
visitor-behavior explanations that its retrieved reports do not establish, including
hedged explanations as review candidates. MATCH semantics come from the selected
contract, rather than the investigator default.

Use this source checkout's `adapters/claude/bin/cardinal-behavior --config /absolute/config.json`
as the command and arguments of an optional Claude MCP server named `cardinal-behavior`.
For example, write a local MCP config (paths must be absolute):

```json
{"mcpServers":{"cardinal-behavior":{"command":"/absolute/cardinal-agent-plugins/adapters/claude/bin/cardinal-behavior","args":["--config","/absolute/config.json"]}}}
```

Then start `claude --mcp-config /absolute/mcp.json` and ask:

> Check whether our investigator agents are claiming they have enough evidence before they submit their report.

The tools are `select_behavior`, `execute_behavior`, `next_behavior_result`, and
`render_storyboard`. The selection returns the frozen contract; execution submits
the accepted version to Cardinal. Polling returns compact findings while execution
runs. Continue polling until a completed receipt is returned, then render it.
The agent gets trace ID, verdict, bounded source-derived reason, witness references,
coverage gaps, and execution status. Raw records, JEV receipts, result locations,
and worker details stay in private host files and the expandable HTML artifact.

The renderer reuses the HTML body from the proven `experiments/behavior_stream.py`
prototype with a deployed receipt loader. The selected version and preview are
verified by their content hashes; the default artifacts are copied from PR #1892.
Each result must identify that version, UDF, profile, and adapter. Rendering requires
all population results and matching final counts. Polling never evaluates traces
and uses the server's cursor unchanged.

This preview is not added to the installed plugin or published release automatically.
The optional source-checkout launcher leaves the existing Cardinal MCP connection intact.

Run focused checks with:

```sh
python3 -m unittest discover -s common/behavior/tests -v
```
