"""Small process-boundary helpers. No core/telemetry imports on the fast path."""

import json
import sys
import threading
import time


def bounded(fn, deadline):
    """Bound imports, file/lock waits and subprocesses as well as network calls.

    Callers must exit with os._exit after flushing: abandoned daemon work
    must not hold the process open or emit output after the deadline.
    """
    if time.monotonic() >= deadline:
        return
    def work():
        try:
            fn()
        except Exception:
            pass
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))


def deliver(results):
    """One Claude JSON response; mark disclosures only after successful flush."""
    messages, contexts, callbacks = [], [], []
    for outputs, after_write in results:
        for out in outputs:
            if out.get("systemMessage"):
                messages.append(out["systemMessage"])
            context = out.get("hookSpecificOutput", {}).get("additionalContext")
            if context:
                contexts.append(context)
        callbacks.extend(after_write)
    out = {}
    if messages:
        out["systemMessage"] = "\n".join(messages)
    if contexts:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "\n\n".join(contexts)}
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
        for callback in callbacks:
            callback()
