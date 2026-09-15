from __future__ import annotations

import json
import unittest

import support
from support import ADAPTER_DIR

from cardinal_core import decisions as core
from cardinal_devin.telemetry import build_decisions, build_decisions_stable, decision_attrs

SCHEMA_PATH = ADAPTER_DIR / "playbook" / "decision-output.schema.json"
PLAYBOOK_PATH = ADAPTER_DIR / "playbook" / "cardinal-decisions.md"


class BuildDecisionsTests(unittest.TestCase):
    def test_valid_entries(self) -> None:
        output = support.load_fixture("v1_sessions.json")["sessions"][0]["structured_output"]
        built, skipped, problem = build_decisions(output)
        self.assertIsNone(problem)
        self.assertEqual([d["id"] for d in built], ["retry-backoff", "environment-variables"])
        first, second = built
        self.assertEqual(first["alternatives"], ["Fixed 5 second delay", "No retry"])
        self.assertEqual(first["anchors"], [
            {"kind": "file", "identifier": "src/sync/retry.py", "path": "src/sync/retry.py"},
            {"kind": "symbol", "identifier": "SyncClient.fetch", "path": "src/sync/client.py"},
        ])
        self.assertEqual(second["decided_by"], "user")
        self.assertEqual(second["links"], [{"relation": "follows_from", "to": "retry-backoff"}])
        self.assertEqual(len(skipped), 2)
        self.assertIn("decisions[2]: choice is required", skipped[0])
        self.assertIn("decisions[3]: anchors[0].kind", skipped[1])

    def test_invalid_entries_are_skipped_with_reasons(self) -> None:
        output = {"decisions": [
            "not an object",
            {"choice": "x", "decided_by": "robot"},
            {"choice": "x", "alternatives": "one"},
            {"choice": "x", "anchors": [{"kind": "file", "identifier": "../etc/passwd"}]},
            {"choice": "x", "anchors": [{"kind": "symbol"}]},
            {"choice": "x", "id": "Not An Id!"},
            {"choice": "self", "id": "self", "refines": ["self"]},
            {"choice": 12},
            {"choice": "   "},
            {"choice": "ok"},
        ]}
        built, skipped, problem = build_decisions(output)
        self.assertIsNone(problem)
        self.assertEqual([d["id"] for d in built], ["ok"])
        self.assertEqual(len(skipped), 9)
        joined = "\n".join(skipped)
        for text in ("entry must be an object", "decided_by must be one of", "alternatives must be an array",
                     "must stay inside the repo", "identifier is required", "is not a decision id",
                     "cannot link to itself", "choice must be a string", "choice is required"):
            self.assertIn(text, joined)

    def test_missing_or_malformed_structured_output(self) -> None:
        for output, text in ((None, "no structured_output"), ([], "not an object"),
                             ({}, "no 'decisions' array"), ({"decisions": {}}, "not an array")):
            built, skipped, problem = build_decisions(output)
            self.assertEqual((built, skipped), ([], []))
            self.assertIn(text, problem)

    def test_same_id_revises_and_auto_ids_stay_unique(self) -> None:
        built, _, _ = build_decisions({"decisions": [
            {"id": "cache", "choice": "Redis"},
            {"id": "cache", "choice": "In-process LRU"},
            {"choice": "Retry"},
            {"choice": "retry!", "question": "different choice, same slug"},
        ]})
        self.assertEqual([(d["id"], d["choice"]) for d in built], [
            ("cache", "In-process LRU"), ("retry", "Retry"), ("retry-2", "retry!"),
        ])

    def test_derived_ids_are_stable_against_prior_decisions(self) -> None:
        built, _, _, keys = build_decisions_stable({"decisions": [{"choice": "Retry"}, {"choice": "retry!"}]})
        self.assertEqual([d["id"] for d in built], ["retry", "retry-2"])
        prior = dict(keys)
        swapped, _, _, _ = build_decisions_stable(
            {"decisions": [{"choice": "retry!"}, {"choice": "Retry"}]}, prior_auto=prior, prior_ids=prior)
        self.assertEqual({d["id"]: d["choice"] for d in swapped}, {"retry-2": "retry!", "retry": "Retry"})
        # A new id-less choice never takes an id already sent, explicit or derived.
        added, _, _, _ = build_decisions_stable(
            {"decisions": [{"choice": "RETRY?"}]}, prior_auto=prior, prior_ids=list(prior) + ["retry-3"])
        self.assertEqual(added[0]["id"], "retry-4")
        # An explicit id in the same output is not taken by a derived one.
        mixed, _, _, _ = build_decisions_stable({"decisions": [{"choice": "Retry"}, {"id": "retry", "choice": "x"}]})
        self.assertEqual([d["id"] for d in mixed], ["retry-2", "retry"])

    def test_directory_anchor_and_path_cleanup(self) -> None:
        built, _, _ = build_decisions({"decisions": [{"choice": "x", "anchors": [
            {"kind": "directory", "identifier": "./src/sync/"},
            {"kind": "config", "identifier": "RETRY_MAX", "path": "/deploy/values.yaml"},
        ]}]})
        self.assertEqual(built[0]["anchors"], [
            {"kind": "directory", "identifier": "src/sync", "path": "src/sync"},
            {"kind": "config", "identifier": "RETRY_MAX", "path": "deploy/values.yaml"},
        ])

    def test_decision_attributes_carry_pr_repo_branch(self) -> None:
        built, _, _ = build_decisions({"decisions": [{"choice": "Use backoff"}]})
        attrs = decision_attrs("sess", built[0], {
            "cardinal_repo": "acme/widgets", "cardinal_branch": "feat/x", "cardinal_head_sha": "abc",
            "cardinal_pr_number": 42, "cardinal_pr_url": "https://github.com/acme/widgets/pull/42",
        })
        self.assertEqual(attrs["cardinal.repo"], "acme/widgets")
        self.assertEqual(attrs["cardinal.branch"], "feat/x")
        self.assertEqual(attrs["cardinal.pr_number"], 42)
        self.assertEqual(attrs["cardinal.decision.id"], "use-backoff")
        self.assertIsNone(attrs["cardinal.decision.code_clusters"])


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = SCHEMA_PATH.read_bytes()
        cls.schema = json.loads(cls.raw)
        cls.item = cls.schema["properties"]["decisions"]["items"]

    def test_draft7_and_size(self) -> None:
        self.assertEqual(self.schema["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertLess(len(self.raw), 64 * 1024)
        self.assertEqual(self.schema["type"], "object")
        self.assertEqual(self.schema["required"], ["decisions"])
        self.assertEqual(self.schema["properties"]["decisions"]["type"], "array")

    def test_refs_are_internal(self) -> None:
        refs = []

        def walk(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    refs.append(node["$ref"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.schema)
        self.assertTrue(refs)
        for ref in refs:
            self.assertTrue(ref.startswith("#/definitions/"), ref)
            self.assertIn(ref.split("/")[-1], self.schema["definitions"])

    def test_matches_core_limits(self) -> None:
        props = self.item["properties"]
        self.assertEqual(self.item["required"], ["id", "choice"])
        self.assertEqual(props["question"]["maxLength"], core.MAX_QUESTION)
        self.assertEqual(props["choice"]["maxLength"], core.MAX_CHOICE)
        self.assertEqual(props["rationale"]["maxLength"], core.MAX_RATIONALE)
        self.assertEqual(props["alternatives"]["maxItems"], core.MAX_ALTERNATIVES)
        self.assertEqual(props["alternatives"]["items"]["maxLength"], core.MAX_ALTERNATIVE)
        self.assertEqual(props["anchors"]["maxItems"], core.MAX_ANCHORS)
        self.assertEqual(tuple(props["decided_by"]["enum"]), core.DECIDED_BY)
        self.assertEqual(tuple(self.schema["definitions"]["anchor"]["properties"]["kind"]["enum"]), core.ANCHOR_KINDS)
        self.assertEqual(self.schema["definitions"]["decisionId"]["pattern"], core.DECISION_ID_RE.pattern)
        for relation in core.LINK_RELATIONS:
            self.assertEqual(props[relation]["maxItems"], core.MAX_LINKS)

    def test_playbook_names_every_field(self) -> None:
        text = PLAYBOOK_PATH.read_text(encoding="utf-8")
        for name in self.item["properties"]:
            self.assertIn(f"`{name}`", text)

    def test_playbook_example_is_valid_input(self) -> None:
        text = PLAYBOOK_PATH.read_text(encoding="utf-8")
        example = json.loads(text.split("```json", 1)[1].split("```", 1)[0])
        built, skipped, problem = build_decisions(example)
        self.assertEqual((len(built), skipped, problem), (1, [], None))


if __name__ == "__main__":
    unittest.main()
