# Import a completed qualification job

`admit` is an operator CLI action. It does not expose a certificate upload route
through HTTP/MCP and does not start discovery, compilation, qualification or SDK
execution. Use an operator-owned completed canonical job and an independently
accepted SHA256 for its `bundle/frozen-qualified-manifest.json`.

The trusted target configuration must include `admission_contracts`, a mapping
from agent ID to the independently provisioned contract JSON file. The contract
must include its qualified `description`. Do not populate this mapping from an
untrusted imported job or use its bundled contract as the target authority.

```sh
python3 -m compilation.cli --config /operator/host.json admit \
  --job-dir /operator/completed-jobs/JOB_ID \
  --agent-id AGENT_ID --bundle-digest ACCEPTED_SHA256
```

Import verifies the completed outcome, PASS certificate and its pinned files,
exact qualified-manifest bytes, candidate version, target catalog, contract,
SDK, JEV and qualification policy. It copies regular files into private target
custody, rejects symlinks/special files/issuer keys, verifies the copy again,
then calls the unchanged `QualifiedRegistry.issue`. That issuer rechecks the
existing independent audit and gate and signs the record with the target key.
Do not transfer a source registry's issuer key.

The original 32-character job directory name must be preserved. Repeating an
identical import returns the same target qualified ID and rechecks the signed
record, evidence and live identities. Different bytes under that job ID,
disabled/revoked records and superseded admissions fail closed. A new target
issuance has a new qualified ID; the original executable, qualification evidence
and accepted bundle digest do not change.

The source directory and target state are private operator storage. This command
is not a new authority to admit a production bundle; use the deployment's
existing owner authorization and target configuration.
