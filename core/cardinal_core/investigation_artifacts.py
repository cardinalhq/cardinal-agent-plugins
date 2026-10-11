"""Local verification of checkpoint artifacts; Cardinal does not verify hashes."""

from __future__ import annotations
import hashlib
import subprocess
import sys
from pathlib import Path
from . import investigation_events as ie


def verify(conn, inv, target, *, root=None, client):
    if root is None:
        got = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if got.returncode:
            raise ValueError("verify needs --root outside a git worktree")
        root = Path(got.stdout.strip())
    root = Path(root).resolve()
    seq = int(target) if str(target).isdigit() else None
    events, _, _ = ie.read_all(
        conn,
        inv,
        after=max(0, seq - 1) if seq else 0,
        client=client,
        max_pages=1000,
        timeout=30,
        limit=ie.CLI_PAGE_LIMIT,
    )
    matches = (
        [e for e in events if e.get("seq") == seq]
        if seq
        else [e for e in events if (e.get("payload") or {}).get("semantic_id") == ie.normalize_id(target)]
    )
    if not matches:
        raise ValueError("no event with that seq or semantic_id")
    # A semantic id identifies its most recent revision.
    artifacts = (matches[-1].get("payload") or {}).get("artifacts", [])
    artifacts = ie.validate_structured("artifacts", artifacts, "verify")
    failed = False
    for artifact in artifacts:
        path = root / artifact["path"]
        try:
            path.resolve().relative_to(root)  # symlinks cannot escape the requested root
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            status = "match" if digest.hexdigest() == artifact["sha256"] else "MISMATCH"
        except (OSError, ValueError):
            status = "missing"
        failed |= status != "match"
        print(f"{status} {artifact['path']}")
    if not artifacts:
        print("no artifacts recorded")
    return int(failed)
