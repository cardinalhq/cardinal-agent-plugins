#!/usr/bin/env python3
"""One synchronous launch for Cardinal's optional prompt hooks.

Local decision context and staged discovery are merged with relay outcomes
and the owner-input disclosure into ONE JSON object. Network discovery and
owner-input posting run in detached processes. The limits gate stays an
independent blocking hook. All work, including imports, Git and locks, is
bounded from interpreter start; hooks.json adds startup headroom.
"""

import time
STARTED = time.monotonic()

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _prompt_support import bounded, deliver

BUDGET_S = 4.0
FINISH_S = 1.0


COMPONENTS = ("decision-prompt", "owner-input", "owner-relay", "storyboard-discovery")


def collect(payload, results, lock):
    if not isinstance(payload, dict):
        return
    from cardinal_core.machine_prompts import is_claude_machine_prompt
    if payload.get("agent_id") or is_claude_machine_prompt(payload.get("prompt")):
        return
    # Independent components share one interpreter, but a stalled optional
    # feature must not prevent another from returning its context/disclosure.
    import importlib.util
    import threading
    directory = os.path.dirname(os.path.abspath(__file__))

    def run(name):
        outputs, callbacks = [], []
        try:
            spec = importlib.util.spec_from_file_location("_prompt_" + name.replace("-", "_"),
                                                         os.path.join(directory, name + ".py"))
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            options = {"emit": outputs.append}
            if name == "owner-input":
                options.update(after_write=callbacks.append, detached=True)
            elif name == "owner-relay":
                options.update(after_write=callbacks.append)
            elif name == "storyboard-discovery":
                options.update(background=True, deadline=STARTED + BUDGET_S, after_write=callbacks.append)
            adapter.main(payload=payload, **options)
        except Exception:
            pass
        with lock:
            results[name] = (outputs, callbacks)

    workers = [threading.Thread(target=run, args=(name,), daemon=True) for name in COMPONENTS]
    for worker in workers:
        if time.monotonic() >= STARTED + BUDGET_S:
            break
        worker.start()
    for worker in workers:
        if worker.ident is not None:
            worker.join(max(0.0, STARTED + BUDGET_S - time.monotonic()))


def main():
    import threading
    results, lock = {}, threading.Lock()
    def work():
        try:
            collect(json.loads(sys.stdin.read() or "{}"), results, lock)
        except Exception:
            pass
    bounded(work, STARTED + BUDGET_S)
    # Snapshot only completed components. Late workers cannot write stdout.
    with lock:
        ready = [results[name] for name in COMPONENTS if name in results]
    def finish():
        deliver(ready)
        import _hook_debug
        _hook_debug.log("prompt-submit", STARTED, completed=len(ready))
    bounded(finish, STARTED + BUDGET_S + FINISH_S)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    os._exit(0)
