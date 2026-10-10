"""Lossless factoring of repeated source strings for model-only requests.

The original packet remains the citation authority. Segments concatenate without
inserting separators, so no evidence is shortened, summarized or reconstructed.
"""
import json
from .storage import digest


def factor(packet):
    strings = []
    def collect(value):
        if isinstance(value, str) and len(value) >= 1024:
            strings.append(value)
        elif isinstance(value, dict):
            for v in value.values(): collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(packet)
    candidates = set(strings)
    fragments = set()
    for text in strings:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if not isinstance(value, dict): continue
        for key in ('system', 'tools'):
            if key not in value: continue
            for separators in ((',', ':'), (', ', ': ')):
                fragment = json.dumps(value[key], ensure_ascii=False, separators=separators)
                if len(fragment) >= 1024 and fragment in text:
                    candidates.add(fragment)
                    fragments.add(fragment)
    repeated = sorted((s for s in candidates if sum(t.count(s) for t in strings) > 1),
                      key=lambda s: (s not in fragments, -len(s), s))
    definitions = {}
    def pack(value):
        if isinstance(value, str) and len(value) >= 1024:
            segments = [value]
            for fragment in repeated:
                parts = []
                for segment in segments:
                    if not isinstance(segment, str) or fragment not in segment:
                        parts.append(segment); continue
                    key = digest(fragment.encode())
                    definitions[key] = fragment
                    pieces = segment.split(fragment)
                    for i, piece in enumerate(pieces):
                        if i: parts.append({'ref': key})
                        if piece: parts.append(piece)
                segments = parts
            return {'$verbatim_segments': segments} if any(isinstance(s, dict) for s in segments) else value
        if isinstance(value, dict): return {k: pack(v) for k, v in value.items()}
        if isinstance(value, list): return [pack(v) for v in value]
        return value
    packed = pack(packet)
    return {'verbatim_definitions': definitions, 'packet': packed}
