# Semantic DAG viewer

This directory is the canonical source for the shared Codex and Claude
Semantic DAG viewer. Adapter entrypoints use it directly in the monorepo.
`build/release.py` copies `viewer/` into each adapter's required plugin layout
when producing self-contained release artifacts.

Do not add or edit adapter-local viewer copies.
