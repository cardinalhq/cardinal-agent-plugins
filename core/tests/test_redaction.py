"""Unit tests for cardinal_core.redaction — docs/privacy-redaction.md §4/§6.

Run:
    cd core && python3 -m unittest discover tests -v
"""

from __future__ import annotations

import importlib
import os
import unittest

from cardinal_core import redaction


class _redact_env:
    """Context manager: set env vars, reimport cardinal_core.redaction so
    its import-time config knobs (CARDINAL_REDACT_MODE,
    CARDINAL_REDACT_MAX_ATTR_BYTES, CARDINAL_ENV) pick up the new values,
    yield the reloaded module for the duration of the `with` block, then
    restore the original env and reload back to the default state.

    NOTE: restoring on __exit__ (not inside the setup step) matters —
    `importlib.reload` mutates the SAME module object in place, so
    restoring before the caller is done using it would silently undo the
    override before any assertion ran against it.
    """

    def __init__(self, **env: str | None) -> None:
        self._env = env
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._env}
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return importlib.reload(redaction)

    def __exit__(self, *exc_info: object) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(redaction)


class SecretDetectionTests(unittest.TestCase):
    def test_aws_access_key_id(self) -> None:
        cleaned, patterns = redaction.scrub_secrets("key=AKIAIOSFODNN7EXAMPLE end")
        self.assertIn("AWS_ACCESS_KEY_ID", patterns)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", cleaned)

    def test_aws_access_key_id_near_miss(self) -> None:
        # Wrong prefix, right length — should NOT match.
        _, patterns = redaction.scrub_secrets("token=BKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AWS_ACCESS_KEY_ID", patterns)

    def test_github_pat_classic(self) -> None:
        token = "ghp_" + "a" * 36
        cleaned, patterns = redaction.scrub_secrets(f"auth: {token}")
        self.assertIn("GITHUB_PAT", patterns)
        self.assertNotIn(token, cleaned)

    def test_github_pat_fine_grained(self) -> None:
        token = "github_pat_" + "a" * 22 + "_" + "b" * 59
        cleaned, patterns = redaction.scrub_secrets(token)
        self.assertIn("GITHUB_PAT", patterns)
        self.assertNotIn(token, cleaned)

    def test_github_pat_near_miss(self) -> None:
        # 36-char alphanumeric string that isn't a GitHub PAT shape.
        _, patterns = redaction.scrub_secrets("id=" + "a" * 36)
        self.assertNotIn("GITHUB_PAT", patterns)

    def test_slack_token(self) -> None:
        token = "xoxb-1234567890-abcdefgHIJKLM"
        cleaned, patterns = redaction.scrub_secrets(token)
        self.assertIn("SLACK_TOKEN", patterns)
        self.assertNotIn(token, cleaned)

    def test_generic_env_secret_assignment(self) -> None:
        cleaned, patterns = redaction.scrub_secrets("DATABASE_PASSWORD=hunter2")
        self.assertIn("GENERIC_ENV_SECRET_ASSIGNMENT", patterns)
        self.assertNotIn("hunter2", cleaned)

    def test_generic_env_secret_near_miss(self) -> None:
        # KEY-suffixed but lowercase name doesn't match the closed shape.
        _, patterns = redaction.scrub_secrets("database_password=hunter2")
        self.assertNotIn("GENERIC_ENV_SECRET_ASSIGNMENT", patterns)

    def test_jwt_like(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ"
        cleaned, patterns = redaction.scrub_secrets(f"Authorization: {token}")
        self.assertIn("JWT_LIKE", patterns)
        self.assertNotIn(token, cleaned)

    def test_private_key_block(self) -> None:
        cleaned, patterns = redaction.scrub_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIB...")
        self.assertIn("PRIVATE_KEY_BLOCK", patterns)
        self.assertNotIn("-----BEGIN RSA PRIVATE KEY-----", cleaned)

    def test_bearer_token(self) -> None:
        token = "Bearer " + "a" * 25
        cleaned, patterns = redaction.scrub_secrets(f"header: {token}")
        self.assertIn("BEARER_TOKEN", patterns)
        self.assertNotIn(token, cleaned)

    def test_remote_url_userinfo(self) -> None:
        url = "https://x-access-token:ghp_" + "a" * 36 + "@github.com/org/repo.git"
        cleaned, patterns = redaction.scrub_secrets(url)
        self.assertIn("REMOTE_URL_USERINFO", patterns)
        self.assertNotIn("x-access-token", cleaned)

    def test_clean_string_detects_nothing(self) -> None:
        cleaned, patterns = redaction.scrub_secrets("git status && ls -la")
        self.assertEqual(patterns, [])
        self.assertEqual(cleaned, "git status && ls -la")

    def test_empty_and_none_input(self) -> None:
        self.assertEqual(redaction.scrub_secrets(""), ("", []))
        self.assertEqual(redaction.scrub_secrets(None), ("", []))

    def test_known_secret_patterns_constant_is_inspectable(self) -> None:
        names = [name for name, _ in redaction.KNOWN_SECRET_PATTERNS]
        for expected in (
            "AWS_ACCESS_KEY_ID", "GITHUB_PAT", "SLACK_TOKEN",
            "GENERIC_ENV_SECRET_ASSIGNMENT", "JWT_LIKE",
        ):
            self.assertIn(expected, names)


class HashFieldTests(unittest.TestCase):
    def test_stable_across_calls(self) -> None:
        self.assertEqual(redaction.hash_field("hello world"), redaction.hash_field("hello world"))

    def test_different_inputs_differ(self) -> None:
        self.assertNotEqual(
            redaction.hash_field("hello")["hash"], redaction.hash_field("world")["hash"]
        )

    def test_truncation_reports_full_length(self) -> None:
        value = "x" * 5000
        result = redaction.hash_field(value, max_bytes=100)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["length"], 5000)
        # The hash must be over the truncated prefix, not the full value.
        self.assertEqual(result["hash"], redaction.hash_field(value[:100], max_bytes=100)["hash"])

    def test_no_truncation_under_the_cap(self) -> None:
        result = redaction.hash_field("short", max_bytes=4096)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["length"], 5)

    def test_never_returns_original_value(self) -> None:
        secret = "super-secret-command --with=args"
        result = redaction.hash_field(secret)
        self.assertNotIn(secret, result["hash"])
        self.assertEqual(set(result), {"hash", "length", "truncated"})


class RedactCommandTests(unittest.TestCase):
    def test_never_contains_the_command(self) -> None:
        cmd = "curl -H 'Authorization: Bearer " + "x" * 25 + "' https://internal.example.com/api"
        result = redaction.redact_command(cmd)
        for value in result.values():
            self.assertNotIn(cmd, str(value))
        self.assertNotIn(cmd, str(result["command_hash"]))

    def test_bash_class_and_multi(self) -> None:
        result = redaction.redact_command("git status && rm -rf build")
        self.assertEqual(result["bash_class"], "file-write")
        self.assertTrue(result["bash_multi"])

    def test_secret_detected_in_command(self) -> None:
        cmd = "curl -H 'Authorization: Bearer " + "x" * 25 + "' https://example.com"
        result = redaction.redact_command(cmd)
        self.assertIn("BEARER_TOKEN", result["secret_patterns"])

    def test_empty_command(self) -> None:
        result = redaction.redact_command("")
        self.assertIsNone(result["bash_class"])
        self.assertFalse(result["bash_multi"])


class RedactFilePathTests(unittest.TestCase):
    def test_relative_inside_cwd_is_verbatim(self) -> None:
        self.assertEqual(redaction.redact_file_path("src/main.py", "/repo"), "src/main.py")

    def test_absolute_inside_cwd_is_relative_verbatim(self) -> None:
        self.assertEqual(redaction.redact_file_path("/repo/src/main.py", "/repo"), "src/main.py")

    def test_outside_cwd_is_hashed(self) -> None:
        result = redaction.redact_file_path("/etc/passwd", "/repo")
        self.assertTrue(result.startswith("outside-cwd:"))
        self.assertNotIn("/etc/passwd", result)

    def test_home_dir_path_outside_cwd_is_hashed(self) -> None:
        result = redaction.redact_file_path("~/.ssh/config", "/repo")
        self.assertTrue(result.startswith("outside-cwd:"))

    def test_unknown_cwd_is_hashed(self) -> None:
        result = redaction.redact_file_path("src/main.py", None)
        self.assertTrue(result.startswith("outside-cwd:"))

    def test_same_pair_is_stable(self) -> None:
        first = redaction.redact_file_path("/other/repo/file.py", "/repo")
        second = redaction.redact_file_path("/other/repo/file.py", "/repo")
        self.assertEqual(first, second)

    def test_absolute_outside_path_identity_is_cwd_independent(self) -> None:
        # An absolute outside-cwd path has an identity of its own — the
        # same external file (e.g. ~/.ssh/config) clusters as "same
        # file" regardless of which repo/session touched it, not a
        # different placeholder per cwd.
        a = redaction.redact_file_path("/other/repo/file.py", "/repo-a")
        b = redaction.redact_file_path("/other/repo/file.py", "/repo-b")
        self.assertEqual(a, b)

    def test_different_absolute_paths_do_not_collide(self) -> None:
        a = redaction.redact_file_path("/etc/passwd", "/repo")
        b = redaction.redact_file_path("/etc/shadow", "/repo")
        self.assertNotEqual(a, b)

    def test_relative_outside_path_without_cwd_is_stable_on_literal_string(self) -> None:
        first = redaction.redact_file_path("../sibling/file.py", None)
        second = redaction.redact_file_path("../sibling/file.py", None)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, redaction.redact_file_path("../other/file.py", None)
        )

    def test_empty_path_passthrough(self) -> None:
        self.assertEqual(redaction.redact_file_path("", "/repo"), "")
        self.assertIsNone(redaction.redact_file_path(None, "/repo"))


class StripUrlUserinfoTests(unittest.TestCase):
    def test_no_userinfo_unchanged(self) -> None:
        url = "https://github.com/cardinalhq/lakerunner.git"
        self.assertEqual(redaction.strip_url_userinfo(url), url)

    def test_bare_username(self) -> None:
        self.assertEqual(
            redaction.strip_url_userinfo("https://user@github.com/org/repo.git"),
            "https://github.com/org/repo.git",
        )

    def test_username_and_token(self) -> None:
        self.assertEqual(
            redaction.strip_url_userinfo("https://x-access-token:ghp_secrettoken@github.com/org/repo.git"),
            "https://github.com/org/repo.git",
        )

    def test_port_with_userinfo_preserved(self) -> None:
        self.assertEqual(
            redaction.strip_url_userinfo("https://user:pass@host.internal:8443/org/repo.git"),
            "https://host.internal:8443/org/repo.git",
        )

    def test_malformed_url_does_not_raise(self) -> None:
        for bad in ("not a url", "https://", "://broken", ""):
            # Must not raise; output need not be meaningful.
            redaction.strip_url_userinfo(bad)

    def test_none_passthrough(self) -> None:
        self.assertIsNone(redaction.strip_url_userinfo(None))

    def test_scp_style_ssh_remote_untouched(self) -> None:
        # No `://` — the `git@` here is the fixed SSH login name, not a
        # credential; canonical_repo() depends on this exact shape.
        url = "git@github.com:cardinalhq/lakerunner.git"
        self.assertEqual(redaction.strip_url_userinfo(url), url)

    def test_no_secret_substring_survives(self) -> None:
        token = "ghp_" + "s" * 36
        url = f"https://x-access-token:{token}@github.com/org/repo.git"
        cleaned = redaction.strip_url_userinfo(url)
        self.assertNotIn(token, cleaned)
        self.assertNotIn("x-access-token", cleaned)


class RedactToolArgsAndOutputTests(unittest.TestCase):
    def test_bash_never_contains_command(self) -> None:
        cmd = "cat ~/.aws/credentials"
        result = redaction.redact_tool_args("Bash", {"cmd": cmd})
        for value in result.values():
            self.assertNotIn(cmd, str(value))
        self.assertEqual(result["bash_class"], "file-read")

    def test_generic_args_are_hashed_not_verbatim(self) -> None:
        patch_text = "*** Update File: secret.py\n+API_KEY = 'sk-verbatim-should-not-appear'"
        result = redaction.redact_tool_args("Edit", {"patch": patch_text})
        for value in result.values():
            self.assertNotIn(patch_text, str(value))
        self.assertIn("args_hash", result)
        self.assertIn("args_length", result)

    def test_empty_args(self) -> None:
        result = redaction.redact_tool_args("Edit", {})
        self.assertEqual(result, {"secret_patterns": []})

    def test_output_never_verbatim(self) -> None:
        stdout = "total 42\ndrwxr-xr-x  .env DATABASE_PASSWORD=hunter2"
        result = redaction.redact_tool_output(stdout)
        for value in result.values():
            self.assertNotIn("hunter2", str(value))
        self.assertIn("GENERIC_ENV_SECRET_ASSIGNMENT", result["secret_patterns"])

    def test_none_output(self) -> None:
        self.assertEqual(redaction.redact_tool_output(None), {"secret_patterns": []})


class StrictModeTests(unittest.TestCase):
    def test_redact_prompt_returns_none_in_strict_mode(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="strict") as mod:
            self.assertIsNone(mod.redact_prompt("some prompt text"))

    def test_redact_prompt_hashes_in_standard_mode(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="standard") as mod:
            result = mod.redact_prompt("some prompt text")
            self.assertIsInstance(result, dict)
            self.assertIn("hash", result)

    def test_invalid_mode_falls_back_to_standard(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="bogus") as mod:
            self.assertEqual(mod.REDACT_MODE, "standard")


class PermissiveModeTests(unittest.TestCase):
    def test_refused_without_cardinal_env_dev(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="permissive", CARDINAL_ENV=None) as mod:
            self.assertFalse(mod.PERMISSIVE_ACTIVE)
            self.assertIsNone(mod.permissive_verbatim("target", "/some/path", frozenset({"target"})))

    def test_refused_with_cardinal_env_prod(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="permissive", CARDINAL_ENV="prod") as mod:
            self.assertFalse(mod.PERMISSIVE_ACTIVE)

    def test_active_only_with_mode_and_dev_env_together(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="permissive", CARDINAL_ENV="dev") as mod:
            self.assertTrue(mod.PERMISSIVE_ACTIVE)
            self.assertEqual(
                mod.permissive_verbatim("target", "/some/path", frozenset({"target"})),
                "/some/path",
            )
            # Not on the allowlist -> still refused even though active.
            self.assertIsNone(mod.permissive_verbatim("command", "rm -rf /", frozenset({"target"})))

    def test_dev_env_alone_without_permissive_mode_is_inactive(self) -> None:
        with _redact_env(CARDINAL_REDACT_MODE="standard", CARDINAL_ENV="dev") as mod:
            self.assertFalse(mod.PERMISSIVE_ACTIVE)


class MaxAttrBytesTests(unittest.TestCase):
    def test_default(self) -> None:
        with _redact_env(CARDINAL_REDACT_MAX_ATTR_BYTES=None) as mod:
            self.assertEqual(mod.MAX_ATTR_BYTES, 4096)

    def test_configured_value_is_honored(self) -> None:
        with _redact_env(CARDINAL_REDACT_MAX_ATTR_BYTES="128") as mod:
            self.assertEqual(mod.MAX_ATTR_BYTES, 128)
            result = mod.redact_command("x" * 500)
            self.assertTrue(result["command_hash"]["truncated"])

    def test_invalid_value_falls_back_to_default(self) -> None:
        with _redact_env(CARDINAL_REDACT_MAX_ATTR_BYTES="not-a-number") as mod:
            self.assertEqual(mod.MAX_ATTR_BYTES, 4096)


if __name__ == "__main__":
    unittest.main()
