---
name: cardinal-status
description: Check the Cardinal Copilot CLI connection, the owner-input setting, and probe configured telemetry and MCP endpoints.
---

# Cardinal Status

Use this skill when the user asks whether Copilot CLI is connected to Cardinal, whether Cardinal telemetry or MCP is configured, whether their prompts are recorded as owner input, or to verify Cardinal setup.

The script is `scripts/cardinal-status` in the Cardinal plugin directory (two levels up from this skill's directory):

```bash
python3 <plugin directory>/scripts/cardinal-status
```

**Sandbox.** The script reads `~/.copilot` and probes Cardinal's endpoints over the network. With Copilot CLI's command sandbox on (`/sandbox enable`; off by default) the probes fail inside it: ask the user to approve a bypass for this command, or give them the command for their own terminal. Don't report a network failure from inside the sandbox as a broken connection.

Surface the script output clearly. The `Owner input:` line says whether this machine's Copilot CLI sessions record typed prompts. Only the user turns that on, in their own terminal, with `cardinal-connect --owner-input on`. If the output says a new session is needed, tell the user that Copilot CLI loads MCP servers at session start.
