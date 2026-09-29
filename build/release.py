#!/usr/bin/env python3
"""Release an adapter to its mirror repo.

The mirror repos (cardinal-{claude,codex,cursor,gemini}-plugin) are where
users install from; development happens in this monorepo (spec §Repo shape
and release flow). This script builds one adapter's artifact — product
code plus vendored cardinal_core, minus monorepo-only files — and pushes
it to the mirror as a release commit + tag.

A composed adapter (a directory with a compose.json, e.g.
adapters/claude-storyboards) is not copied whole: its artifact is the
explicit `include` list taken from its `base` adapter, with the composed
directory's own files (plugin.json, .mcp.json, hooks.json, README) laid
over it. Two plugins can share one mirror (the claude mirror carries
plugins/cardinal and plugins/cardinal-storyboards); each has its own tag
prefix and its own marketplace.json entry, matched by exact plugin name.

Usage:
    python3 build/release.py <adapter> [--dry-run] [--work-dir DIR]
    python3 build/release.py <adapter> --build-only DIR   # lay the artifact down, no git

Version comes from the adapter's plugin.json manifest. The push is direct
to the mirror's main; if the mirror enforces PRs, the script pushes a
release/v<version> branch and prints the PR command instead.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
CORE_PKG = ROOT / "core" / "cardinal_core"
ADAPTERS_DIR = ROOT / "adapters"


class Mirror(NamedTuple):
    slug: str        # mirror repo
    subpath: str     # plugin directory inside the mirror
    tag_prefix: str  # release tag = tag_prefix + version; unique per plugin in a mirror


MIRRORS = {
    "claude": Mirror("cardinalhq/cardinal-claude-plugin", "plugins/cardinal", "v"),
    # Second plugin in the claude mirror. Its own tag namespace: the mirror
    # already has v0.1.0 … v0.34.x tags from the full plugin.
    "claude-storyboards": Mirror("cardinalhq/cardinal-claude-plugin", "plugins/cardinal-storyboards",
                                 "cardinal-storyboards/v"),
    "codex": Mirror("cardinalhq/cardinal-codex-plugin", "plugins/cardinal-codex-plugin", "v"),
    "cursor": Mirror("cardinalhq/cardinal-cursor-plugin", "plugins/cardinal-cursor-plugin", "v"),
    "gemini": Mirror("cardinalhq/cardinal-gemini-plugin", "plugins/cardinal-gemini-plugin", "v"),
}

# Monorepo-only files never shipped to mirrors.
EXCLUDE = {"tests", "REPORT.md", "CORE_GAPS.md", "__pycache__"}
# A composed adapter's build recipe (never shipped).
COMPOSE_FILE = "compose.json"

REQUIRED_ARTIFACTS = {
    "claude": (".claude-plugin/plugin.json",),
    "claude-storyboards": (
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "hooks/hooks.json",
        "hooks/_plugin_mode.py",
        "hooks/cardinal_core/evidence.py",
        "bin/cardinal-evidence",
        "skills/storyboard/SKILL.md",
        "skills/canvas/SKILL.md",
        "skills/canvas/scripts/render_preview.py",
    ),
    "codex": (".codex-plugin/plugin.json",),
}

# ${CLAUDE_PLUGIN_ROOT}/<path> in a hooks.json command.
PLUGIN_ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s\"']+)")

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
    return json.loads(Path(matches[0]).read_text())["version"]


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE}


def compose_spec(adapter: str) -> dict | None:
    """adapters/<adapter>/compose.json, or None for a whole-directory adapter."""
    path = ADAPTERS_DIR / adapter / COMPOSE_FILE
    if not path.is_file():
        return None
    spec = json.loads(path.read_text())
    base, include = spec.get("base"), spec.get("include")
    if not isinstance(base, str) or not (ADAPTERS_DIR / base).is_dir() or (ADAPTERS_DIR / base / COMPOSE_FILE).exists():
        raise RuntimeError(f"{adapter}/{COMPOSE_FILE}: base must name a whole-directory adapter")
    if not isinstance(include, list) or not include or not all(isinstance(i, str) and i for i in include):
        raise RuntimeError(f"{adapter}/{COMPOSE_FILE}: include must be a non-empty list of paths")
    return spec


def _safe_rel(rel: str, adapter: str) -> Path:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or rel != p.as_posix():
        raise RuntimeError(f"{adapter}/{COMPOSE_FILE}: include path must be relative and normalized: {rel!r}")
    return p


def _compose(adapter: str, spec: dict, dest: Path) -> None:
    """The composed artifact: each `include` path from the base adapter (a
    file, or a directory copied whole minus EXCLUDE), then every file of the
    composed adapter's own directory laid over it."""
    base = ADAPTERS_DIR / spec["base"]
    dest.mkdir(parents=True)
    for rel in spec["include"]:
        p = _safe_rel(rel, adapter)
        src = base / p
        if any(part in EXCLUDE for part in p.parts):
            raise RuntimeError(f"{adapter}/{COMPOSE_FILE}: {rel} is monorepo-only")
        target = dest / p
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=_ignore)
        elif src.is_file():
            shutil.copy2(src, target)
        else:
            raise RuntimeError(f"{adapter}/{COMPOSE_FILE}: {rel} does not exist in adapters/{spec['base']}")
    own = ADAPTERS_DIR / adapter
    for src in sorted(own.rglob("*")):
        rel = src.relative_to(own)
        if not src.is_file() or rel.as_posix() == COMPOSE_FILE or any(part in EXCLUDE for part in rel.parts):
            continue
        if rel.parts[:2] == ("hooks", "cardinal_core"):
            continue  # a stray vendored copy; the fresh one is added below
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def build_artifact(adapter: str, dest: Path) -> None:
    """Copy adapter product code + vendored core into dest."""
    src = ADAPTERS_DIR / adapter
    if dest.exists():
        shutil.rmtree(dest)

    spec = compose_spec(adapter)
    if spec is not None:
        _compose(adapter, spec, dest)
    else:
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
    validate_artifact(adapter, dest)


def validate_artifact(adapter: str, dest: Path) -> None:
    """Fail a release before tagging when required packaged assets are absent."""
    missing = [
        relative
        for relative in REQUIRED_ARTIFACTS.get(adapter, ())
        if not (dest / relative).is_file() or not (dest / relative).stat().st_size
    ]
    if missing:
        raise RuntimeError(
            f"{adapter} release artifact is missing required files: "
            + ", ".join(missing)
        )
    # Every hook command must name a file the artifact ships (a composed
    # plugin's include list can otherwise drift from its hooks.json).
    hooks_json = dest / "hooks" / "hooks.json"
    if hooks_json.is_file():
        dangling = sorted({
            ref for ref in PLUGIN_ROOT_REF.findall(hooks_json.read_text())
            if not (dest / ref).is_file()
        })
        if dangling:
            raise RuntimeError(
                f"{adapter} release artifact: hooks.json runs files it does not ship: "
                + ", ".join(dangling)
            )


def plugin_manifest(dest: Path) -> dict:
    """The built artifact's plugin.json (.claude-plugin/, .codex-plugin/, ...)."""
    matches = sorted(dest.glob(".*plugin/plugin.json"))
    return json.loads(matches[0].read_text()) if matches else {}


def sync_marketplace(marketplace_json: Path, manifest: dict, subpath: str, label: str = "") -> bool:
    """Set the version of the marketplace.json entry whose name is EXACTLY
    this plugin's name (two plugins share the claude mirror's marketplace:
    `cardinal` and `cardinal-storyboards`; neither may touch the other's
    entry). Adds the entry when the plugin has none yet (its first release).
    Returns whether the file changed."""
    name, version = manifest.get("name"), manifest.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise RuntimeError(f"{label}: built plugin.json has no name/version")
    mf = json.loads(marketplace_json.read_text())
    plugins = mf.setdefault("plugins", [])
    entries = [e for e in plugins if isinstance(e, dict) and e.get("name") == name]
    changed = False
    if not entries:
        entry = {
            "name": name,
            "version": version,
            "description": manifest.get("description", ""),
            "source": f"./{subpath}",
            "category": "observability",
            "homepage": manifest.get("homepage", ""),
            "license": manifest.get("license", "Apache-2.0"),
        }
        plugins.append({k: v for k, v in entry.items() if v != ""})
        print(f"{label}: marketplace.json adds {name} {version}")
        changed = True
    for entry in entries:
        if entry.get("version") != version:
            print(f"{label}: marketplace.json {name} {entry.get('version')} → {version}")
            entry["version"] = version
            changed = True
    marketplace_json.write_text(json.dumps(mf, indent=2) + "\n")
    return changed


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
    parser.add_argument("--build-only", metavar="DIR", default=None,
                        help="Only build the artifact into DIR (no clone, no git)")
    args = parser.parse_args()

    adapter = args.adapter
    if args.build_only:
        build_artifact(adapter, Path(args.build_only))
        print(f"{adapter}: built artifact in {args.build_only}")
        return 0
    slug, subpath, tag_prefix = MIRRORS[adapter]
    version = adapter_version(adapter)
    tag = f"{tag_prefix}{version}"
    mono_sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)

    workdir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix=f"mirror-{adapter}-"))
    clone = workdir / slug.split("/")[1]
    if clone.exists():
        shutil.rmtree(clone)
    run(["gh", "repo", "clone", slug, str(clone), "--", "--depth", "1"])

    # The full ref: a bare pattern also matches a tail ("v0.2.0" would match
    # cardinal-storyboards/v0.2.0 and skip the full plugin's release).
    existing_tags = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=clone)
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
        sync_marketplace(marketplace_json, plugin_manifest(plugin_dir), subpath, adapter)

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
    # Try to open the PR. Two independent failure shapes we care about:
    #   (a) PR already exists for this head — fine, look it up and continue.
    #   (b) Anything else — annotate with the actual stderr (was previously
    #       swallowed by run()'s check=True raising a plain CalledProcessError
    #       that carried the stderr but never printed it).
    create_cmd = [
        "gh", "pr", "create",
        "-R", slug,
        "--head", branch,
        "--base", "main",
        "--title", f"release {tag}",
        "--body", pr_body,
    ]
    create_result = subprocess.run(
        create_cmd, cwd=clone, capture_output=True, text=True,
    )
    if create_result.returncode == 0:
        pr_url = create_result.stdout.strip()
        print(f"{adapter}: {slug} main is protected; opened {pr_url}")
    elif "already exists" in (create_result.stderr or ""):
        # (a) look up the existing PR and continue with auto-merge
        try:
            pr_url = run([
                "gh", "pr", "list", "-R", slug, "--head", branch,
                "--state", "open", "--json", "url", "-q", ".[0].url",
            ], cwd=clone).strip()
            if not pr_url:
                # "already exists" but list returned nothing — likely
                # already-merged (or closed). Nothing to do; the branch is
                # pushed, mirror main will pick it up on the next matching
                # release or manual merge.
                print(
                    f"::warning title=Release PR gone but tag exists::"
                    f"{adapter}/{slug}: 'gh pr create' said PR exists "
                    f"but 'gh pr list' returned nothing. Likely closed "
                    f"without merge. Check {slug} PRs for {branch}."
                )
                return 1
            print(f"{adapter}: {slug} main is protected; existing PR {pr_url}")
        except subprocess.CalledProcessError as list_exc:
            print(
                f"::warning title=Release PR lookup failed::"
                f"{adapter}/{slug}: {list_exc.stderr or list_exc}"
            )
            return 1
    else:
        # (b) real failure — auth, permissions, network, etc. Surface the
        # actual gh stderr so we can debug what went wrong instead of the
        # opaque CalledProcessError print v1 shipped.
        print(
            f"::warning title=Release PR create failed::"
            f"{adapter}/{slug}: pushed {branch} + {tag}, but "
            f"'gh pr create' returned {create_result.returncode}.\n"
            f"stderr: {create_result.stderr.strip() or '(empty)'}\n"
            f"stdout: {create_result.stdout.strip() or '(empty)'}\n"
            f"Manual: gh pr create -R {slug} --head {branch} "
            f"--base main --title 'release {tag}'"
        )
        return 1

    # Merge strategy: try immediate squash first. Mirror repos have no
    # required status checks (release-mirrors.yml already ran the tests
    # before this step), so the PR is instantly mergeable. `gh pr merge
    # --auto` is designed to wait for required checks and is flaky when
    # there's nothing to wait for — sometimes silently no-ops leaving the
    # PR open. Fall back to `--auto` only if immediate merge fails, and
    # then to human intervention if that fails too.
    try:
        run(["gh", "pr", "merge", pr_url, "--squash"], cwd=clone)
        print(f"{adapter}: merged immediately: {pr_url}")
    except subprocess.CalledProcessError:
        try:
            run(["gh", "pr", "merge", pr_url, "--auto", "--squash"], cwd=clone)
            print(f"{adapter}: auto-merge enabled on {pr_url}")
        except subprocess.CalledProcessError:
            print(
                f"{adapter}: neither immediate merge nor auto-merge succeeded; "
                f"land the PR manually: {pr_url}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
