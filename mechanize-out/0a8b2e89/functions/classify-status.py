# Stage 12 iterate-once: body filled in from the stub's own sketch. The
# transformation is regex extraction of two labeled markdown lines
# (`**Overall Status:** ...`, `**Indicator:** ...`) plus a fixed map from
# the statuspage.io indicator vocabulary onto {operational, degraded,
# incident, unknown}. Deterministic, small, and pattern-like; the only
# reason CORE.md Stage 7 called for a stub here is that this pattern
# (markdown-anchor-line extract) isn't enumerated in M1/M2/M3.
import re

_INDICATOR_TO_LEVEL = {
    "none": "operational",
    "maintenance": "operational",
    "minor": "degraded",
    "major": "incident",
    "critical": "incident",
}


def run(inp):
    text = inp["response"]["text"]
    indicator = _match_first(text, r'\*\*Indicator:\*\*\s*"?([A-Za-z_-]+)"?')
    description = _match_first(text, r'\*\*Overall Status:\*\*\s*([^\n]+)')
    level = _INDICATOR_TO_LEVEL.get(indicator or "", "unknown")
    return {
        "indicator": indicator or "unknown",
        "description": (description or "unknown").strip(),
        "level": level,
    }


def _match_first(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None
