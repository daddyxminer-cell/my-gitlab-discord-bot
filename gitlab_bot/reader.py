"""
Reads the inside of one GitLab project: file list, README / docs, and a few key source files.
This is the part that makes the bot "search in project files" rather than only name/description.

Cost per project: 1 request (root listing) + 1 (README) for most projects; only projects that already
look like a chain/node get the deeper read (~4-10 more requests).
"""
from __future__ import annotations

import logging
import re

import patterns as P
from classify import Repo, prelim_score, tree_hits
from gl import GitLab

log = logging.getLogger("gl-bot")

README_RE = re.compile(r"^(readme|read_me|readme\.\w+)(\.(md|markdown|rst|txt|adoc|org))?$", re.I)
DOC_EXT = (".md", ".markdown", ".rst", ".txt", ".adoc")
# Folders worth listing in depth (coins keep their consensus / node code in one of these)
DEEP_DIRS = ("src", "lib", "core", "node", "crates", "pkg", "cmd", "internal", "chain", "consensus",
             "blockchain", "daemon", "docs", "doc", "app", "x", "runtime", "pallets", "programs")
MAX_DEEP_DIRS = 5
MAX_KEY_FILES = 7
MAX_DOC_FILES = 3

# Ordered by how much they tell us. First match per rule wins; the list is then capped.
KEY_FILE_RULES = [
    re.compile(r"(^|/)chainparams\w*\.(cpp|h|rs|go)$|(^|/)chain_?params\.(rs|go|py|json|toml)$", re.I),
    re.compile(r"(^|/)pow\.(cpp|h|rs|go|c|py|js|ts|java)$|(^|/)consensus/params\.(h|rs|go)$", re.I),
    re.compile(r"(^|/)genesis\w*\.(json|toml|rs|go|cpp|h|yaml|yml|py)$", re.I),
    re.compile(r"(^|/)(params|config|consensus)\.(go|rs)$", re.I),
    re.compile(r"(^|/)miner\w*\.(cpp|h|rs|go|py)$|(^|/)mining\w*\.(cpp|h|rs|go|py)$", re.I),
    re.compile(r"(^|/)masternode\w*\.(cpp|h|rs|go)$", re.I),
    re.compile(r"(^|/)(testnet|devnet|network)\w*\.(toml|json|ya?ml|conf)$|(^|/)(config|node|node_config)\.(toml|ya?ml|json|conf)$", re.I),
    re.compile(r"(^|/)Cargo\.toml$", re.I),
]
DOC_RULES = [
    re.compile(r"(^|/)(docs?|documentation)/.*(testnet|node|validator|mining|miner|run|setup|install|whitepaper|roadmap|consensus|tokenomics).*\.(md|rst|txt)$", re.I),
    re.compile(r"(^|/)(whitepaper|roadmap|tokenomics|testnet|mining|masternode|node|validator)\w*\.(md|rst|txt)$", re.I),
]


def _is_blob(e: dict) -> bool:
    return e.get("type") == "blob"


def read_project(g: GitLab, repo: Repo) -> None:
    """Fill repo.paths / repo.text / repo.code / repo.files_read. Raises Unreadable / Throttled on API trouble."""
    ref = repo.default_branch
    repo.skip_reason = None
    if repo.empty or not ref:
        repo.skip_reason = "empty repository"
        return

    root, _ = g.tree(repo.id, ref)
    if not root:
        repo.skip_reason = "no files"
        return
    repo.paths = [e["path"] for e in root]

    # --- README ---
    texts: list[str] = []
    readme = next((e["path"] for e in root if _is_blob(e) and README_RE.match(e["name"])), None)
    if readme:
        t = g.raw_file(repo.id, readme, ref, limit=60_000)
        if t:
            texts.append(t)
            repo.files_read.append(readme)
    repo.text = "\n\n".join(texts)

    root_hits = tree_hits(repo.paths)
    interesting = prelim_score(repo) >= 2 or any(root_hits.values())
    if not interesting:
        return

    # --- deeper: list the main source / docs folders ---
    deep = [e["path"].rstrip("/") for e in root
            if e.get("type") == "tree" and e["name"].lower() in DEEP_DIRS][:MAX_DEEP_DIRS]
    # `src` first: that is where chainparams.cpp / pow.cpp live
    deep.sort(key=lambda p: (p.lower() not in ("src", "crates", "core"), p.lower()))
    for d in deep:
        entries, _ = g.tree(repo.id, ref, d, recursive=True, max_pages=2)
        repo.paths.extend(e["path"] for e in entries)

    # --- key files ---
    blobs = [p for p in repo.paths if not p.endswith("/") and not P.IGNORED_PATH.search(p)]
    chosen: list[str] = []
    for rx in KEY_FILE_RULES:
        for p in blobs:
            if rx.search(p) and p not in chosen:
                chosen.append(p)
                break
    chosen = chosen[:MAX_KEY_FILES]
    docs: list[str] = []
    for rx in DOC_RULES:
        for p in blobs:
            if rx.search(p) and p not in docs and p not in repo.files_read:
                docs.append(p)
                if len(docs) >= MAX_DOC_FILES:
                    break
        if len(docs) >= MAX_DOC_FILES:
            break

    for p in chosen:
        t = g.raw_file(repo.id, p, ref, limit=60_000)
        if t:
            repo.code[p] = t
            repo.files_read.append(p)
    extra = []
    for p in docs:
        t = g.raw_file(repo.id, p, ref, limit=40_000)
        if t:
            extra.append(t)
            repo.files_read.append(p)
    if extra:
        repo.text = (repo.text + "\n\n" + "\n\n".join(extra)).strip()
