# Core gaps found while building the omnigent adapter (P5)

Core 0.2.0 held up: connection/identity/state-as-arguments (the
§omnigent constraints) all worked exactly as designed — no shims, no
forks. Two minor gaps, both worked around adapter-side; neither blocked
anything.

## 1. No Anthropic pricing table in `pricing.py`

`pricing.PROVIDER_TABLES` covers OpenAI and Gemini only (by design: the
CLI Claude adapter gets `cost_usd` from Claude Code natively). omnigent
drives Claude-family harnesses through a server, so when the engine's
cumulative `context.usage.total_cost_usd` is absent (engine-dependent),
there is no fallback price for Anthropic SKUs and `cost_usd` is skipped.

**Workaround:** the cost-delta path (`total_cost_usd` anchor via
`state_updates`) is primary; `_fallback_cost` iterates
`PROVIDER_TABLES` and honestly emits nothing for unpriced models (no
misleading $0 rows). **Future core rev:** an Anthropic table keyed like
the others would close the residual hole for engines that never
populate `total_cost_usd`.

## 2. `otlp.resource_attrs` cannot carry adapter-specific extras

The spec requires `cardinal.omnigent_harness` on the resource (slice by
underlying harness). `resource_attrs(...)` takes only the fixed keyword
set; there is no `extra:` parameter.

**Workaround:** mutate the returned dict
(`attrs["cardinal.omnigent_harness"] = harness` in
`telemetry._resource`). Cosmetic — the dict is plain and ours — but an
`extra_attrs: dict[str, str] | None = None` parameter would keep
adapters out of the construction details.
