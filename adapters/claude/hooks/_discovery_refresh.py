"""Detached discovery refresh. Only the foreground consumes pending context.

The lease coalesces overlapping prompts, expires after a crashed worker,
and contains no credentials. Worker stdin carries only session/cwd, never
prompt text or keys. The worker rechecks connection and opt-out settings.
"""

import json
import os
import sys
import time
from pathlib import Path


def spawn(sid, cwd):
    from cardinal_core.paths import safe_session
    directory = Path.home() / ".claude" / "cardinal" / "storyboard-discovery"
    directory.mkdir(parents=True, exist_ok=True)
    lease = directory / (safe_session(sid) + ".refresh.lock")
    try:
        if lease.exists() and time.time() - lease.stat().st_mtime > 60:
            lease.unlink()
        fd = os.open(str(lease), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        import subprocess
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(lease)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        try:
            process.stdin.write(json.dumps({"sid": sid, "cwd": cwd}).encode())
        finally:
            process.stdin.close()
        return True
    except Exception:
        lease.unlink(missing_ok=True)
        return False


def refresh(spec, deadline):
    import _connection
    import _storyboard_discovery as wiring
    if not _connection.is_connected() or wiring.is_disabled():
        return
    from cardinal_core import storyboard_authoring as sa
    sa.relook_if_moved(sa.agent_paths("claude"), "claude", spec["sid"], spec["cwd"],
                       connection=wiring.connection, client=wiring.client_header,
                       deadline=deadline, deliver_by=0.0, stage_only=True)


if __name__ == "__main__":
    started = time.monotonic()
    from _prompt_support import bounded
    try:
        spec = json.loads(sys.stdin.read())
        bounded(lambda: refresh(spec, started + 8.0), started + 8.0)
    except Exception:
        pass
    finally:
        try:
            Path(sys.argv[1]).unlink(missing_ok=True)
        except OSError:
            pass
    os._exit(0)
