#!/usr/bin/env python3
"""Regenerate adapters/devin/tests/goldens/*.json.

Each golden is the OTLP bodies one poll cycle posts for the synthetic
fixtures, with version stamps normalized. tests/test_contract.py reads
these to check the cross-adapter contract.

    PYTHONPATH=core python3 adapters/devin/tests/capture_goldens.py
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable, Dict, List, Tuple

import support

# name -> (api, use GitHub token)
SCENARIOS: Dict[str, Tuple[str, bool]] = {
    "poll_v1_github": ("v1", True),
    "poll_v1_no_token": ("v1", False),
    "poll_v3_github": ("v3", True),
}
NORMALIZED = "<normalized>"


def normalize(body: Dict[str, Any]) -> Dict[str, Any]:
    body = copy.deepcopy(body)
    for rl in body["resourceLogs"]:
        for attr in rl["resource"]["attributes"]:
            if attr["key"] in ("cardinal.plugin_version", "cardinal.core_version"):
                attr["value"] = {"stringValue": NORMALIZED}
        for sl in rl["scopeLogs"]:
            sl["scope"]["version"] = NORMALIZED
    return body


def capture(name: str) -> Dict[str, Any]:
    api, use_github = SCENARIOS[name]
    cleanups: List[Tuple[Callable[..., Any], tuple]] = []
    try:
        harness = support.Harness(lambda fn, *args: cleanups.append((fn, args)), api=api)
        harness.poller(github=use_github).poll_once()
        return {"posts": [normalize(b) for b in harness.ingest.accepted_bodies()]}
    finally:
        for fn, args in reversed(cleanups):
            fn(*args)


def main() -> int:
    logging.getLogger("cardinal_devin").setLevel(logging.ERROR)
    support.GOLDENS.mkdir(exist_ok=True)
    for name in SCENARIOS:
        path = support.GOLDENS / f"{name}.json"
        path.write_text(json.dumps(capture(name), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
