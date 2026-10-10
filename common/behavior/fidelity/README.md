# Pre-gold source fidelity

This module is an additive campaign boundary. It does not change the frozen
compiler, SDK, JEV, semantic contract, policy or qualification gate.

`campaign.before_gold(root, cases, frozen_recorded_bases, gold_worker)` writes
`fidelity/receipts.json` and refuses to call the gold worker unless every case
passes. `campaign.before_predictions` requires identical passing receipts and
refuses an existing `qualification/` directory. A campaign must additionally
assert its frozen files and sealed commitment before predictions.

Each case has `trace`, `capture`, and `derivation`. The derivation binds an
original recorded trace path/hash, source receipt path/hash, source identity,
recorder identity and ordered transforms. The operator enrolls these identities
in a frozen recorded-base catalog before corpus construction. A case cannot
enroll its own base. Attested companion files must still match the receipt.
Canonical UTF-8 JSON bytes of replayed and submitted trace objects must agree.
String bodies, including provider request JSON, remain exact captured text.

The allowlist permits final response text edits, span removal with explicit loss,
root/other output redaction or truncation with forced flags, and undeclared scope.
It forbids provider request edits and protocol-field deletion. These transforms
are declared synthetic derivations, never represented as new real executions.

Anthropic adapter reference: installed `@anthropic-ai/sdk` **0.110.0**, file
`src/resources/messages/messages.ts`, interfaces `ToolResultBlockParam`,
`ToolUseBlockParam`, `MessageParam` and `Message`. The type requires
`tool_use_id: string`. Adjacency, complete next-turn answers, ID uniqueness and
span timing/content are explicit additional campaign constraints; the type file
alone does not prove provider runtime enforcement of those constraints. The
adapter is structural and makes no claim of an actual provider rejection.

Legacy OpenInference spans may declare only the operation name `Anthropic
Messages`; that is an explicit dispatch alias. Unknown provider/operation pairs
fail closed. No agent-specific business rules enter this module.

Run tests with:

```sh
PYTHONPATH=common/behavior python3 -m unittest discover -s common/behavior/tests -p test_source_fidelity.py -v
```

The regression reads the original sealed campaign 2 r12 and provenance bundle
without changing either. Historical fidelity audits must distinguish request
protocol failures from absent independent recorded-base derivation. A missing
derivation alone is not evidence that a provider rejected a request.
