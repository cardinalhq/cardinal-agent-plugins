"""Smoke tests for the kopf handlers wiring (M4).

These tests never touch a cluster and never invoke ``kopf.run``. They verify
only the wiring around the pure reconciler:

* the ``handlers`` module imports cleanly (all decorators register);
* the exported handler callables are present;
* calling ``reconcile()`` with a monkeypatched git-clone and a fake k8s client
  ends up invoking ``BatchV1Api.create_namespaced_job`` with the manifest that
  the pure reconciler returned.
"""
from __future__ import annotations

import base64
import json
import types
from pathlib import Path

import pytest
import yaml

# Skip the whole module if the runtime deps aren't installed — this file is
# also runnable outside the controller container where ``kopf`` /
# ``kubernetes`` are not present.
kopf = pytest.importorskip("kopf")
k8s_client_mod = pytest.importorskip("kubernetes.client")


def _write_sentinel_dir(root: Path) -> None:
    """Materialize a minimal Sentinel directory that reads as a valid clone."""
    (root / "sentinel.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sentinels.cardinalhq.io/v1alpha1",
                "kind": "Sentinel",
                "spec": {
                    "capabilities": {
                        "required": [{"id": "observability.list-services"}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "inputs.json").write_text(
        json.dumps({"instance": "prod-us-east-2"}), encoding="utf-8"
    )
    (root / "deployment.yaml").write_text(
        yaml.safe_dump({"sinks": [{"id": "stdout"}]}), encoding="utf-8"
    )


def test_module_imports_and_exports_handlers():
    import handlers

    assert callable(handlers.reconcile)
    assert callable(handlers.on_delete)
    assert callable(handlers.on_job_event)


def test_reconcile_creates_job_with_reconciler_manifest(monkeypatch, tmp_path):
    """End-to-end wiring: git clone monkeypatched, k8s clients replaced with
    recorders — verify ``reconcile()`` calls ``create_namespaced_job`` with the
    Job manifest that ``reconciler.reconcile_sentinel`` produced.
    """
    import handlers
    from reconciler import reconcile_sentinel

    # --- Stub git clone to point at a tmp dir with a minimal Sentinel dir. ---
    fake_repo = tmp_path / "repo"
    sentinel_dir_relpath = "sentinels/svc"
    (fake_repo / sentinel_dir_relpath).mkdir(parents=True)
    _write_sentinel_dir(fake_repo / sentinel_dir_relpath)

    def _fake_clone(url, ref, *, cache_root=None):  # noqa: ARG001
        return fake_repo

    monkeypatch.setattr(handlers, "_git_clone_cached", _fake_clone)

    # --- Recording fake CoreV1Api / BatchV1Api. ---
    created_secrets: list[dict] = []
    created_jobs: list[dict] = []
    created_cronjobs: list[dict] = []

    class FakeCore:
        def create_namespaced_secret(self, namespace, body):
            created_secrets.append({"namespace": namespace, "body": body})

    class FakeBatch:
        def create_namespaced_job(self, namespace, body):
            created_jobs.append({"namespace": namespace, "body": body})

        def create_namespaced_cron_job(self, namespace, body):
            created_cronjobs.append({"namespace": namespace, "body": body})

    monkeypatch.setattr(handlers.k8s_client, "CoreV1Api", FakeCore)
    monkeypatch.setattr(handlers.k8s_client, "BatchV1Api", FakeBatch)

    # --- Assemble CR fields kopf would have handed the handler. ---
    spec = {
        "source": {
            "git": {
                "url": "https://example.invalid/repo",
                "ref": "main",
                "path": sentinel_dir_relpath,
            },
        },
        "inputs": {"serviceQuery": "lakerunner"},
        "capabilities": [
            {
                "id": "observability.list-services",
                "provider": "mcp",
                "endpointSecretRef": "mcp-endpoint",
                "tokenSecretRef": "mcp-token",
            },
        ],
        "sinks": [{"id": "stdout"}],
        "runtime": {"image": "ghcr.io/cardinalhq/sentinel-executor:v0.1.0"},
    }
    meta = {
        "name": "svc-health",
        "namespace": "svc",
        "uid": "abc-uid-123",
        "generation": 1,
    }

    # kopf hands the handler a ``patch`` object with ``.status`` acting as a
    # dict-like accumulator. Simulate the shape the handler writes into.
    class PatchStub:
        def __init__(self):
            self.status: dict = {}

    patch = PatchStub()

    # --- Invoke the wrapped handler directly. ---
    # kopf decorators return the original function (they only register
    # metadata), so ``handlers.reconcile`` is callable with our stub kwargs.
    handlers.reconcile(spec=spec, meta=meta, status={}, patch=patch)

    # --- Assert: exactly one Job created, no CronJob, and its manifest is
    # what the pure reconciler produced from the same spec. ---
    assert len(created_jobs) == 1, created_jobs
    assert created_cronjobs == []
    assert len(created_secrets) == 1

    expected = reconcile_sentinel({
        "apiVersion": "sentinels.cardinalhq.io/v1alpha1",
        "kind": "Sentinel",
        "metadata": meta,
        "spec": spec,
    })
    assert expected.job_or_cronjob is not None

    created = created_jobs[0]
    assert created["namespace"] == "svc"
    assert created["body"]["kind"] == "Job"
    assert created["body"]["metadata"]["name"] == expected.job_or_cronjob["metadata"]["name"]
    # The manifest handed to the k8s client is the *exact* object the pure
    # reconciler returned — no re-wrapping in the handler.
    assert created["body"] is expected.job_or_cronjob or (
        created["body"] == expected.job_or_cronjob
    )

    # Projected secret carries the dir-side inputs merged under the CR ones.
    inputs_b64 = created_secrets[0]["body"]["data"]["inputs.json"]
    merged_inputs = json.loads(base64.b64decode(inputs_b64).decode("utf-8"))
    assert merged_inputs["instance"] == "prod-us-east-2"  # from dir
    assert merged_inputs["serviceQuery"] == "lakerunner"  # from CR

    # Status parked at Reconciling until on_job_event mirrors the owned
    # Job's real terminal state onto the CR. Running/Succeeded/Failed are
    # driven by the child Job's status, not by reaching the end of the
    # create-time reconcile.
    assert patch.status["phase"] == "Reconciling"
    assert patch.status["observedGeneration"] == 1


def test_reconcile_blocks_when_capability_binding_missing(monkeypatch, tmp_path):
    """If the sentinel dir declares a required capability that the CR does
    not bind, the handler must set phase=Blocked and raise PermanentError
    (kopf stops retrying until the CR spec changes).
    """
    import handlers

    fake_repo = tmp_path / "repo"
    (fake_repo / "sentinels/svc").mkdir(parents=True)
    (fake_repo / "sentinels/svc/sentinel.yaml").write_text(
        yaml.safe_dump({
            "spec": {
                "capabilities": {
                    "required": [{"id": "observability.error-overview"}],
                }
            }
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        handlers, "_git_clone_cached",
        lambda url, ref, cache_root=None: fake_repo,
    )
    # Any k8s call in this path is a bug; make them explode loudly.
    class Explode:
        def __getattr__(self, _name):
            raise AssertionError("k8s API touched in Blocked path")

    monkeypatch.setattr(handlers.k8s_client, "CoreV1Api", Explode)
    monkeypatch.setattr(handlers.k8s_client, "BatchV1Api", Explode)

    spec = {
        "source": {"git": {
            "url": "https://example.invalid/repo",
            "ref": "main",
            "path": "sentinels/svc",
        }},
        "capabilities": [],  # nothing bound → required id is missing
    }
    meta = {"name": "svc", "namespace": "svc", "uid": "u", "generation": 2}

    class PatchStub:
        def __init__(self): self.status: dict = {}

    patch = PatchStub()

    with pytest.raises(kopf.PermanentError):
        handlers.reconcile(spec=spec, meta=meta, status={}, patch=patch)

    assert patch.status["phase"] == "Blocked"
    assert patch.status["observedGeneration"] == 2
    assert any(c.get("type") == "CapabilitiesBound" and c.get("status") == "False"
               for c in patch.status.get("conditions", []))
