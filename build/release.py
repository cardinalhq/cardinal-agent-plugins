#!/usr/bin/env python3
"""Release an adapter to its mirror repo.

The mirror repos (cardinal-{claude,codex,cursor,gemini}-plugin) are where
users install from; development happens in this monorepo (spec §Repo shape
and release flow). This script builds one adapter's artifact — product
code plus vendored cardinal_core, minus monorepo-only files — and pushes
it to the mirror as a release commit + tag.

Usage:
    python3 build/release.py <adapter> [--dry-run] [--work-dir DIR]

Version comes from the adapter's plugin.json manifest. The push is direct
to the mirror's main; if the mirror enforces PRs, the script pushes a
release/v<version> branch and prints the PR command instead.
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_PKG = ROOT / "core" / "cardinal_core"

# adapter -> (mirror repo slug, plugin subpath inside the mirror)
MIRRORS = {
    "claude": ("cardinalhq/cardinal-claude-plugin", "plugins/cardinal"),
    "codex": ("cardinalhq/cardinal-codex-plugin", "plugins/cardinal-codex-plugin"),
    "cursor": ("cardinalhq/cardinal-cursor-plugin", "plugins/cardinal-cursor-plugin"),
    "gemini": ("cardinalhq/cardinal-gemini-plugin", "plugins/cardinal-gemini-plugin"),
}

# Monorepo-only files never shipped to mirrors.
EXCLUDE = {"tests", "REPORT.md", "CORE_GAPS.md", "__pycache__"}

BANNER = (
    "> [!NOTE]\n"
    "> This repository is a **release mirror**. Development happens in\n"
    "> [cardinal-agent-plugins](https://github.com/cardinalhq/cardinal-agent-plugins)"
    " — send PRs there.\n"
)


def run(args: list[str], cwd: Path | None = None) -> str:
    out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def adapter_version(adapter: str) -> str:
    matches = glob.glob(str(ROOT / "adapters" / adapter / ".*plugin" / "plugin.json"))
    if not matches:
        sys.exit(f"no plugin.json manifest found for adapter {adapter!r}")
    return json.load(open(matches[0]))["version"]


def build_artifact(adapter: str, dest: Path) -> None:
    """Copy adapter product code + vendored core into dest."""
    src = ROOT / "adapters" / adapter
    if dest.exists():
        shutil.rmtree(dest)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in EXCLUDE}

    shutil.copytree(src, dest, ignore=_ignore)
    # Vendored core goes next to hooks/ (bin/ for claude has its own
    # sys.path bootstrap into hooks/).
    vendor_dest = dest / "hooks" / "cardinal_core"
    if vendor_dest.exists():
        shutil.rmtree(vendor_dest)
    shutil.copytree(CORE_PKG, vendor_dest, ignore=shutil.ignore_patterns("__pycache__"))
    # Plugin-level LICENSE and .gitignore ship in every artifact even when
    # the adapter dir doesn't carry them (marketplaces expect LICENSE; the
    # ignore file keeps user checkouts from committing pyc noise).
    if not (dest / "LICENSE").exists():
        shutil.copy(ROOT / "LICENSE", dest / "LICENSE")
    if not (dest / ".gitignore").exists():
        (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n")


def ensure_banner(readme: Path) -> None:
    if not readme.exists():
        return
    text = readme.read_text()
    if "release mirror" in text:
        return
    lines = text.split("\n")
    # Insert after the title line.
    insert_at = 1 if lines and lines[0].startswith("#") else 0
    lines.insert(insert_at, "\n" + BANNER)
    readme.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter", choices=sorted(MIRRORS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--work-dir", default=None,
                        help="Reuse a directory for the mirror clone (default: temp)")
    args = parser.parse_args()

    adapter = args.adapter
    slug, subpath = MIRRORS[adapter]
    version = adapter_version(adapter)
    tag = f"v{version}"
    mono_sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)

    workdir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix=f"mirror-{adapter}-"))
    clone = workdir / slug.split("/")[1]
    if clone.exists():
        shutil.rmtree(clone)
    run(["gh", "repo", "clone", slug, str(clone), "--", "--depth", "1"])

    existing_tags = run(["git", "ls-remote", "--tags", "origin", tag], cwd=clone)
    if existing_tags:
        # v29-follow-up: was sys.exit(non-zero) — hostile to matrix jobs on
        # push, where only the adapter with the actual version bump has a
        # new tag and the other three would fail loudly. No-op with a
        # message is idempotent, matches release-mirrors.yml's push-trigger
        # semantics (fire all 4 on any adapter's version bump; only the
        # bumped one actually releases).
        print(f"{adapter}: {slug} already at {tag} — no-op")
        return 0

    plugin_dir = clone / subpath
    # Remove the old plugin package (including its stale tests) and lay
    # down the new artifact. Mirror files OUTSIDE the plugin subpath
    # (README, docs/, LICENSE) are preserved, EXCEPT marketplace.json
    # which is updated below to sync its declared version.
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    build_artifact(adapter, plugin_dir)
    # Mirror-level stale tests referencing the old layout, if present at
    # the plugin dir level only, were removed with the subpath above.
    ensure_banner(clone / "README.md")

    # Sync .claude-plugin/marketplace.json's declared version with the
    # plugin.json version we just laid down. Claude Code's `/plugin`
    # command reads marketplace.json to determine what version to offer;
    # if it stays stale, users see no update available even after the
    # plugin folder itself has advanced. Skip cleanly when the file
    # doesn't exist (codex/cursor/gemini mirrors are for their respective
    # CLIs — no Claude Code marketplace manifest at the root).
    marketplace_json = clone / ".claude-plugin" / "marketplace.json"
    if marketplace_json.exists():
        mf = json.loads(marketplace_json.read_text())
        for entry in mf.get("plugins", []):
            if entry.get("source", "").rstrip("/").endswith(f"/{adapter}") or \
               entry.get("name") == adapter or entry.get("name") == "cardinal":
                if entry.get("version") != version:
                    print(
                        f"{adapter}: marketplace.json {entry['name']} "
                        f"{entry.get('version')} → {version}"
                    )
                    entry["version"] = version
        marketplace_json.write_text(json.dumps(mf, indent=2) + "\n")

    # Vendored core must be committed in mirrors even though the monorepo
    # gitignores it — guard against an inherited ignore rule.
    gi = clone / ".gitignore"
    if gi.exists() and "cardinal_core" in gi.read_text():
        text = "\n".join(
            l for l in gi.read_text().splitlines() if "cardinal_core" not in l
        )
        gi.write_text(text + "\n")

    run(["git", "add", "-A"], cwd=clone)
    status = run(["git", "status", "--porcelain"], cwd=clone)
    if not status:
        print(f"{adapter}: mirror already up to date at {version}")
        return 0

    msg = (
        f"release {tag}: built from cardinal-agent-plugins@{mono_sha}\n\n"
        f"Adapter code now consumes cardinal-agent-core "
        f"(vendored at {subpath}/hooks/cardinal_core). "
        f"Development happens in the cardinal-agent-plugins monorepo."
    )
    if args.dry_run:
        print(f"[dry-run] would commit to {slug}:")
        print(run(["git", "status", "--short"], cwd=clone)[:2000])
        return 0

    run(["git", "commit", "-q", "-m", msg], cwd=clone)
    run(["git", "tag", tag], cwd=clone)
    try:
        run(["git", "push", "-q", "origin", "HEAD:main", tag], cwd=clone)
        print(f"{adapter}: released {tag} to {slug} (direct push)")
        return 0
    except subprocess.CalledProcessError:
        pass

    # main is protected — push a release branch + tag, then open (and
    # auto-merge) a PR. Previously this step just printed the gh command,
    # which meant nobody ran it and stale release/vX.Y.Z branches piled up
    # while the marketplace's main stayed frozen. See cardinal-agent-plugins
    # release.py-auto-pr PR for why.
    branch = f"release/{tag}"
    run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}", tag], cwd=clone)

    pr_body = (
        f"Advances main to {tag} (from cardinal-agent-plugins@{mono_sha}).\n\n"
        f"Auto-created by build/release.py because {slug} main is "
        f"protected and cannot be pushed to directly.\n\n"
        f"Safe to squash-merge — the branch is a full snapshot rebuild."
    )
    try:
        pr_url = run([
            "gh", "pr", "create",
            "-R", slug,
            "--head", branch,
            "--base", "main",
            "--title", f"release {tag}",
            "--body", pr_body,
        ], cwd=clone).strip()
        print(f"{adapter}: {slug} main is protected; opened {pr_url}")
    except subprocess.CalledProcessError as exc:
        # gh will error if a PR already exists for this head → fine, look it up.
        try:
            existing = run([
                "gh", "pr", "list", "-R", slug, "--head", branch,
                "--state", "open", "--json", "url", "-q", ".[0].url",
            ], cwd=clone).strip()
            pr_url = existing or f"(gh pr list failed after auto-create errored: {exc})"
            print(f"{adapter}: {slug} main is protected; existing PR {pr_url}")
        except subprocess.CalledProcessError:
            # Emit as a GitHub Actions warning annotation so it surfaces in
            # the workflow UI, not just buried in stdout. (::warning:: is a
            # no-op on non-Actions runs.)
            print(
                f"::warning title=Release PR needs manual create::"
                f"{adapter}/{slug}: pushed {branch} + {tag}, but "
                f"'gh pr create' failed (likely: GH_TOKEN lacks "
                f"pull-requests:write on {slug} — set MIRROR_PR_TOKEN "
                f"secret). Open manually: "
                f"gh pr create -R {slug} --head {branch} "
                f"--base main --title 'release {tag}'"
            )
            return 1

    # Best-effort enable auto-merge so the PR lands when required checks
    # pass without a human squash click. Silently fine if the repo doesn't
    # allow auto-merge — falls back to the human merging the printed PR.
    try:
        run(["gh", "pr", "merge", pr_url, "--auto", "--squash"], cwd=clone)
        print(f"{adapter}: auto-merge enabled on {pr_url}")
    except subprocess.CalledProcessError:
        # No annotation — this is expected on repos that don't allow
        # auto-merge; the PR still exists and is mergeable manually.
        print(
            f"{adapter}: auto-merge not enabled (repo may not allow it); "
            f"land the PR manually: {pr_url}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
