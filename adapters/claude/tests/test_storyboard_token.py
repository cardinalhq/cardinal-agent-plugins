"""hooks/storyboard-token.py on storyboard__add_act.

storyboard__add_act opens the next act of a published storyboard and returns
create's token block (evidence_token, evidence_token_expires_at,
evidence_upload.path /api/orgs/<org>/storyboards/<id>/evidence) for the SAME
storyboard id, so `cardinal-evidence promote --storyboard <id>` uploads into
the open act exactly as it did for act 1. The create/preview cases live in
test_evidence_promote.py; this file adds the act path.

Requires cardinal_core vendored: python3 build/vendor.py claude
"""

from __future__ import annotations

import json
import re
import unittest

from test_evidence_promote import (  # noqa: F401  (_HomeCase is a base, not a test)
    ORG, PLUGIN_ROOT, SB, SB2, SESSION, TOKEN, TOKEN2, _HomeCase, create_result,
)

ADD_ACT = "mcp__plugin_cardinal_cardinal__storyboard__add_act"


def add_act_result(sb=SB, org=ORG, token=TOKEN2, act=2) -> dict:
    result = create_result(sb=sb, org=org, token=token)
    result.update({"act": act, "title": "Rollback verified", "evidence_token_expires_at": "2099-03-01T00:00:00Z"})
    return result


class AddActTokenTests(_HomeCase):
    def test_add_act_stores_the_token_under_the_same_storyboard_id(self):
        result = add_act_result()
        self.run_hook(ADD_ACT, {"content": json.dumps(result), "structuredContent": result})
        rec = self.ev.read_tokens(self.root, SESSION)[SB]
        self.assertEqual((rec["org"], rec["evidence_token"]), (ORG, TOKEN2))
        self.assertEqual(rec["expires_at"], "2099-03-01T00:00:00Z")

    def test_add_act_refreshes_the_act_one_token_in_place(self):
        # Act 1 was created in this session; act 2 reuses the storyboard id,
        # so there is still exactly one stored storyboard, with the new token.
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result()})
        self.run_hook("mcp__cardinal__storyboard__add_act", [{"type": "text", "text": json.dumps(add_act_result())}])
        tokens = self.ev.read_tokens(self.root, SESSION)
        self.assertEqual(set(tokens), {SB})
        self.assertEqual(tokens[SB]["evidence_token"], TOKEN2)

    def test_add_act_upload_path_for_another_storyboard_is_refused(self):
        result = add_act_result()
        result["evidence_upload"]["path"] = f"/api/orgs/{ORG}/storyboards/{SB2}/evidence"
        self.run_hook(ADD_ACT, {"structuredContent": result})
        self.assertFalse(self.token_file().exists())

    def test_other_servers_cannot_plant_an_add_act_token(self):
        result = add_act_result(org="attacker-org")
        for name in ("mcp__evil__storyboard__add_act", "mcp__plugin_cardinal_cardinal__storyboard__discard_act",
                     "mcp__plugin_cardinal_cardinal__storyboard__find"):
            self.run_hook(name, {"structuredContent": result})
        self.assertFalse(self.token_file().exists())

    def test_matcher_covers_add_act_and_nothing_new_besides(self):
        groups = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        [matcher] = [g["matcher"] for g in groups for h in g["hooks"] if "storyboard-token.py" in h["command"]]
        for name in (ADD_ACT, "mcp__cardinal__storyboard__add_act",
                     "mcp__plugin_cardinal_cardinal__storyboard__create",
                     "mcp__plugin_cardinal_cardinal__storyboard__preview"):
            self.assertTrue(re.fullmatch(matcher, name), name)
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__find",
                     "mcp__plugin_cardinal_cardinal__storyboard__discard_act",
                     "mcp__plugin_cardinal_cardinal__storyboard__publish", "mcp__evil__storyboard__add_act"):
            self.assertFalse(re.fullmatch(matcher, name), name)

    def test_hook_tools_list_names_add_act(self):
        text = (PLUGIN_ROOT / "hooks" / "storyboard-token.py").read_text()
        self.assertIn('TOOLS = ("storyboard__create", "storyboard__preview", "storyboard__add_act")', text)


if __name__ == "__main__":
    unittest.main()
