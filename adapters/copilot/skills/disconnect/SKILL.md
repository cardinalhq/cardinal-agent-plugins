---
name: cardinal-disconnect
description: Disconnect GitHub Copilot CLI from Cardinal by revoking keys and removing the managed Cardinal MCP entries.
---

# Cardinal Disconnect

Use this skill when the user asks to disconnect Cardinal, remove Cardinal telemetry or MCP tools from Copilot CLI, or revoke Cardinal keys.

The script is `scripts/cardinal-disconnect` in the Cardinal plugin directory (two levels up from this skill's directory):

```bash
python3 <plugin directory>/scripts/cardinal-disconnect
```

**Sandbox.** The script revokes keys on Cardinal and edits `~/.copilot`. With Copilot CLI's command sandbox on (`/sandbox enable`; off by default) that fails inside it: ask the user to approve a bypass for this command, or give them the command to run in their own terminal.

If the state file is missing and the user still wants cleanup, rerun with `--force`.

Disconnecting also removes `~/.copilot/cardinal/`, including the owner-input consent: after a later connect, owner input stays off until the user runs `cardinal-connect --owner-input on` again.

After success, tell the user to start a new Copilot CLI session (or `/restart`) so it drops the Cardinal MCP servers. The plugin's hooks stay installed with the plugin and do nothing until the next connect; `copilot plugin uninstall cardinal` removes the plugin itself.
