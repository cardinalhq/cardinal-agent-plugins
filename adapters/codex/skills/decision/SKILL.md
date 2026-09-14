---
name: cardinal-decision
description: Turn Cardinal decision capture on or off for Codex, or show its status and this session's recorded decisions.
---

# Cardinal Decision Capture

Use this skill when the user asks to turn Cardinal decision capture on or off, or asks whether it is on.

Run one of:

```bash
python3 scripts/cardinal-decision on
python3 scripts/cardinal-decision off
python3 scripts/cardinal-decision status
```

Surface the script output. Capture is off by default. When it is on, the Cardinal telemetry hook adds instructions to each prompt telling you the exact `record` command (including `--session`) to run whenever a material decision is made; follow those instructions rather than composing the command yourself.
