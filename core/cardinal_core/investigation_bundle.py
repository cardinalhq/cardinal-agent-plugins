"""Portable, bounded evidence bundles. Hash integrity is not independent claim verification."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from . import investigation_events as ie
from . import investigation_state_sync as sync

MAX_BYTES = 64 * 1024 * 1024


def selected(conn, inv, target, *, client):
    events, _, _ = ie.read_all(conn, inv, after=0, client=client, max_pages=1000, timeout=30, limit=200)
    seq = int(target) if str(target).isdigit() else None
    matches = [e for e in events if ie.event_class(e) == ie.SEMANTIC and (
        e.get("seq") == seq if seq is not None else (e.get("payload") or {}).get("semantic_id") == ie.normalize_id(target))]
    if not matches:
        raise ValueError("no semantic event with that seq or semantic_id")
    return matches


def export(conn, inv, target, out: Path, *, root=None, client):
    if out.exists() or out.is_symlink():
        raise ValueError("bundle destination already exists; choose a new directory")
    events = selected(conn, inv, target, client=client)
    root = Path(root).resolve() if root is not None else None
    stage = Path(tempfile.mkdtemp(prefix=".cardinal-bundle-", dir=out.parent))
    os.chmod(stage, 0o700)
    used, files = 0, []
    manifest = {"version": 1, "investigation_id": inv, "events": events, "files": files,
                "artifacts": [], "receipts": [],
                "verification": "content integrity only; participant claims and run context are not independently verified"}
    def write(name, data):
        nonlocal used
        used += len(data)
        if used > MAX_BYTES: raise ValueError("bundle exceeds 64 MiB; select a smaller event or artifact set")
        file = stage / name; file.parent.mkdir(exist_ok=True)
        file.write_bytes(data); file.chmod(0o600)
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    try:
        ids = sorted({rid for e in events for rid in (e.get("payload") or {}).get("evidence", [])})
        for rid in ids:
            if not ie.RECEIPT_REF_RE.fullmatch(rid): raise ValueError("bundle requires uploaded rcpt_ evidence")
            payload = {"receipt_id": rid}
            if conn.get("token"): payload["investigation_id"] = inv
            receipt = sync._post(conn, "get-receipt", payload, client=client)
            name = f"receipts/{rid}.json"
            write(name, (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode())
            manifest["receipts"].append({"receipt_id": rid, "file": name})
        copied = set()
        for event in events:
            for artifact in ie.validate_structured("artifacts", event["payload"].get("artifacts", []), "bundle"):
                entry = {**artifact, "event_seq": event["seq"], "included": False}
                if root is not None:
                    source = root / artifact["path"]
                    if not source.resolve().is_relative_to(root): raise ValueError("artifact escapes --root")
                    with source.open("rb") as stream: data = stream.read(MAX_BYTES - used + 1)
                    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                        raise ValueError(f"artifact hash mismatch: {artifact['path']}")
                    name = f"artifacts/{artifact['sha256']}"
                    if name not in copied: write(name, data); copied.add(name)
                    entry.update(included=True, file=name)
                manifest["artifacts"].append(entry)
        encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        if used + len(encoded) > MAX_BYTES: raise ValueError("bundle exceeds 64 MiB")
        file = stage / "manifest.json"; file.write_bytes(encoded); file.chmod(0o600)
        seal = stage / "manifest.sha256"
        seal.write_text(hashlib.sha256(encoded).hexdigest() + "\n"); seal.chmod(0o600)
        # No overwrite, including an empty directory created while receipts were fetched.
        out.mkdir(mode=0o700)
        try:
            os.replace(stage, out)
        except BaseException:
            shutil.rmtree(out)
            raise
        print(f"Evidence bundle: {out}; {len(ids)} receipts, {len(copied)} included artifacts. {manifest['verification']}.")
        return 0
    finally:
        if stage.exists(): shutil.rmtree(stage)


def verify(bundle: Path):
    file = bundle / "manifest.json"
    seal = bundle / "manifest.sha256"
    if not file.resolve().is_relative_to(bundle.resolve()) or not seal.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("bundle manifest escapes its directory")
    if file.stat().st_size > MAX_BYTES: raise ValueError("bundle manifest exceeds 64 MiB")
    encoded = file.read_bytes()
    with seal.open() as stream: expected = stream.read(66).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(encoded).hexdigest() != expected:
        raise ValueError("bundle manifest hash mismatch; metadata may have changed")
    manifest = json.loads(encoded)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("unknown bundle format")
    if len(manifest["files"]) > 10000 or not isinstance(manifest.get("artifacts"), list) or not isinstance(manifest.get("receipts"), list):
        raise ValueError("invalid bundle manifest")
    failed, seen, digests, total = False, set(), {}, 0
    for entry in manifest["files"]:
        if not isinstance(entry, dict): raise ValueError("invalid bundle file record")
        name = entry.get("path", "")
        if not isinstance(name, str) or not re.fullmatch(r"(?:receipts/rcpt_[0-9a-f]{24}\.json|artifacts/[0-9a-f]{64})", name) or name in seen:
            raise ValueError("invalid or duplicate bundle file path")
        seen.add(name)
        path = bundle / name
        if not path.resolve().is_relative_to(bundle.resolve()): raise ValueError("bundle file escapes its directory")
        try:
            with path.open("rb") as stream: data = stream.read(MAX_BYTES - total + 1)
            total += len(data)
            if total > MAX_BYTES: raise ValueError("bundle exceeds 64 MiB")
            digest = hashlib.sha256(data).hexdigest(); digests[name] = digest
            match = digest == entry.get("sha256") and len(data) == entry.get("bytes")
        except OSError:
            match = False
        failed |= not match
        print(f"{'integrity match' if match else 'MISMATCH or missing'} {name}")
    for a in manifest.get("artifacts", []):
        if not isinstance(a, dict): raise ValueError("invalid artifact record")
        if a.get("included"):
            failed |= digests.get(a.get("file")) != a.get("sha256")
        else: print(f"artifact not included; not verified: {a.get('path')}")
    for receipt in manifest.get("receipts", []):
        if not isinstance(receipt, dict): raise ValueError("invalid receipt record")
        failed |= receipt.get("file") not in digests
    print("Bundle integrity checks do not independently verify source, remote execution, measurements or claims.")
    return int(failed)
