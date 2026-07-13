# Core gaps observed during the Gemini migration (P2)

No functional gaps: every algorithm the shipped hook implemented had a
drop-in core equivalent (`otlp`, `initiative`, `bashclass`, `pricing` with
`GEMINI_PRICING_USD_PER_M`, `limits.gate_output(hook_event_name="BeforeAgent")`,
`session.*`, `deviceflow`). The two items below are test-harness-level
nits, both worked around adapter-side in `tests/scenarios.py::normalize_extra`.

## 1 — Harness does not normalize `cardinal.plugin_version` / scope version

**Needed:** goldens captured from the shipped repo (plugin.json v0.1.0)
must compare equal to output stamped with the monorepo adapter's version,
which will drift on every release.

**Workaround:** local normalizer pins the `cardinal.plugin_version`
resource-attribute value and the OTel scope `version` to `"<normalized>"`.

**Suggested API:** extend `core/tests/harness.py::_normalize` to pin
`cardinal.plugin_version` (and scope `version`) the same way it already
pins `cardinal.core_version` and `ts` — every P1–P4 adapter needs this
identically.

## 2 — No harness mode for pre-migration goldens missing `cardinal.core_version`

**Needed:** core's `otlp.resource_attrs()` (correctly, by design) appends
a `cardinal.core_version` resource attribute that the pre-migration
plugins never emitted, so pre/post batches can never be byte-equal on the
resource block. The harness pins its *value* but cannot reconcile its
*presence*.

**Workaround:** local normalizer drops the `cardinal.core_version`
attribute from both sides before comparison; a separate assertion in this
migration verified the migrated hook does emit it (value = core's
`CORE_VERSION`).

**Suggested API:** a `normalized_batches(ignore_attrs={...})` parameter, or
a documented "migration mode" that deletes (rather than pins)
`cardinal.core_version`, so P3/P4 don't re-implement the same drop.
