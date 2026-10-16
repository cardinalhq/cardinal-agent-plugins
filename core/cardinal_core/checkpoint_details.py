"""Bounded structured progress and client-attested run provenance."""
import re
import unicodedata

LANE_STATES = ("pending", "running", "passed", "failed", "blocked", "skipped")
RUN_FIELDS = {"host", "source_commit", "image_digest", "command", "started_at", "finished_at"}


def validate(name, value):
    def text(v, cap):
        return isinstance(v, str) and 1 <= len(v) <= cap and bool(v.strip()) and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or unicodedata.category(c) == "Cs" for c in v)
    if name == "lanes":
        if not isinstance(value, list) or not 1 <= len(value) <= 50:
            raise ValueError("lanes is a list of 1-50 lane/H/state objects")
        seen = set()
        for lane in value:
            if not isinstance(lane, dict) or set(lane) - {"lane", "h", "state", "detail"}:
                raise ValueError("each lane takes lane, optional h, state and optional detail")
            if not text(lane.get("lane"), 100) or lane.get("state") not in LANE_STATES:
                raise ValueError("lane requires a name and state pending|running|passed|failed|blocked|skipped")
            if "h" in lane and not text(lane["h"], 100):
                raise ValueError("h is a string of 1-100 characters")
            if "detail" in lane and not text(lane["detail"], 500):
                raise ValueError("lane detail is 1-500 characters without controls")
            key = (lane["lane"], lane.get("h"))
            if key in seen:
                raise ValueError("duplicate lane/H in one checkpoint")
            seen.add(key)
        return [dict(lane) for lane in value]
    if not isinstance(value, dict) or not value or set(value) - RUN_FIELDS:
        raise ValueError("run takes host, source_commit, image_digest, command, started_at, finished_at")
    for field, v in value.items():
        if not text(v, 1000 if field == "command" else 200):
            raise ValueError(f"run {field} is bounded nonempty text without controls")
        if field == "source_commit" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v):
            raise ValueError("source_commit is a full lowercase commit hash")
        if field == "image_digest" and not re.fullmatch(r"sha256:[0-9a-f]{64}", v):
            raise ValueError("image_digest is sha256: followed by 64 lowercase hex characters")
    return dict(value)
