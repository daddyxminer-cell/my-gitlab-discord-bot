"""
Pure scoring logic (no network): turns what we read from a project into a verdict.

    repo = Repo(...); repo.text = README+docs; repo.paths = [...]; repo.code = {path: text}
    classify(repo, min_score=8, include_node=True)
    -> repo.kind  "pow" | "node" | None
       repo.stage "pre-testnet" | "testnet" | "mainnet"
       repo.score, repo.algo, repo.reasons

Evidence comes from three places (see patterns.py): TEXT, TREE (file names), CODE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import patterns as P


def _compile(d: dict) -> dict:
    return {k: re.compile(v, re.I) for k, v in d.items()}


POW_RE = _compile(P.POW_PATTERNS)
CHAIN_STRONG_RE = _compile(P.CHAIN_STRONG)
CHAIN_WEAK_RE = _compile(P.CHAIN_WEAK)
NODE_RE = _compile(P.NODE_PATTERNS)
NEG_RE = _compile(P.NEG_PATTERNS)
TREE_POW_RE = _compile(P.TREE_POW)
TREE_CHAIN_RE = _compile(P.TREE_CHAIN)
TREE_NODE_RE = _compile(P.TREE_NODE)
CODE_POW_RE = _compile(P.CODE_POW)
CODE_CHAIN_RE = _compile(P.CODE_CHAIN)
CODE_NODE_RE = _compile(P.CODE_NODE)
CODE_TOY_RE = _compile(P.CODE_TOY)
HEAD_CHAIN_RE = re.compile(P.HEAD_CHAIN, re.I)
UPSTREAM_TITLE = re.compile(
    r"^\s*#?\s*(bitcoin|litecoin|dogecoin|dash|monero|zcash|raven(coin)?|firo|verus|kaspa|ethereum|go-ethereum|geth|"
    r"bitcoin cash|bitcoin sv|namecoin|peercoin|digibyte|vertcoin)( core| node| project| daemon)?\s*"
    r"(integration/staging tree|reference implementation)?\s*$", re.I | re.M)

POW_W = {"proof-of-work": 1, "algorithm": 2, "mining": 2, "miner": 1, "pool": 1, "hashrate": 1}
TREE_W = {"file:chainparams": 4}
CODE_POW_W = {"code:pow-params": 4, "code:algo": 2, "code:mining": 2}
CODE_CHAIN_W = {"code:genesis": 3, "code:magic": 3, "code:subsidy": 3, "code:address": 2, "code:ports": 2, "code:net": 1}
CODE_NODE_W = {"code:masternode": 3, "code:validator": 2, "code:p2p": 1}


@dataclass
class Repo:
    id: int
    full_name: str               # group/project
    url: str
    owner: str                   # top-level namespace
    description: str = ""
    topics: list = field(default_factory=list)
    stars: int = 0
    created_at: str = ""
    pushed_at: str = ""          # last_activity_at
    default_branch: str | None = None
    readme_url: str | None = None
    fork: bool = False
    mirror: bool = False
    archived: bool = False
    empty: bool = False
    # filled while reading the project
    text: str = ""               # README + docs
    paths: list = field(default_factory=list)       # file / folder paths seen
    code: dict = field(default_factory=dict)         # key file path -> content
    files_read: list = field(default_factory=list)
    # verdict
    kind: str | None = None      # "pow" | "node" | None
    stage: str = "pre-testnet"
    score: int = 0
    algo: str | None = None
    reasons: list = field(default_factory=list)
    flags: dict = field(default_factory=dict)        # internal booleans used by the bot (pow_ev, real_chain ...)
    skip_reason: str | None = None
    llm: dict | None = None
    origin: str = "new"           # "new" = found by creation date, "active" = found by recent activity
    recency: object = None       # recency.Recency (set by recency.assess)

    @classmethod
    def from_api(cls, d: dict) -> "Repo":
        ns = d.get("namespace") or {}
        full = d["path_with_namespace"]
        owner = (ns.get("full_path") or full.rsplit("/", 1)[0]).split("/")[0]
        return cls(
            id=int(d["id"]), full_name=full, url=d["web_url"], owner=owner,
            description=d.get("description") or "",
            topics=d.get("topics") or d.get("tag_list") or [],
            stars=int(d.get("star_count") or 0), created_at=d.get("created_at") or "",
            pushed_at=d.get("last_activity_at") or "",
            default_branch=d.get("default_branch"), readme_url=d.get("readme_url"),
            fork=bool(d.get("forked_from_project")), mirror=bool(d.get("mirror")),
            archived=bool(d.get("archived")), empty=bool(d.get("empty_repo")),
        )


def _hits(text: str, patterns: dict) -> set[str]:
    return {name for name, rx in patterns.items() if rx.search(text)}


def strip_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", " ", text, flags=re.S)


def tree_hits(paths: list[str]) -> dict[str, set[str]]:
    """Which file-name signals fire (vendored / test paths are ignored)."""
    real = [p for p in paths if not P.IGNORED_PATH.search(p)]
    out = {"pow": set(), "chain": set(), "node": set()}
    for p in real:
        for kind, table in (("pow", TREE_POW_RE), ("chain", TREE_CHAIN_RE), ("node", TREE_NODE_RE)):
            for name, rx in table.items():
                if rx.search(p):
                    out[kind].add(name)
    root = {p.rstrip("/").lower() for p in paths if "/" not in p.rstrip("/")}
    if "configure.ac" in root and "src" in root and (root & {"depends", "contrib", "share", "autogen.sh"}):
        out["chain"].add("layout:bitcoin-fork")
    return out


def detect_stage(text: str, flags: dict) -> str:
    """pre-testnet | testnet | mainnet  (from README/docs text)."""
    if P.MAINNET_LIVE.search(text) or P.MAINNET_FACTS.search(text):
        return "mainnet"
    if P.TESTNET_LIVE.search(text):
        return "testnet"
    return "pre-testnet"


def classify(repo: Repo, min_score: int, include_node: bool) -> None:
    name = re.sub(r"[-_.]+", " ", repo.full_name.split("/")[-1])
    head = " ".join([name, repo.description, " ".join(t.replace("-", " ") for t in repo.topics)])
    full_text = (repo.text or "")[:120000]
    prose = strip_code_fences(full_text)
    code_blob = "\n".join(repo.code.values())[:400000]

    # ---------------- TEXT ----------------
    pow_h, pow_b = _hits(head, POW_RE), _hits(prose, POW_RE)
    pow_all = pow_h | pow_b
    pow_text = sum(POW_W[k] + (1 if k in pow_h else 0) for k in pow_all)
    if pow_all and P.ALGO_WEAK_RE.search(head + " " + prose):
        pow_text += 1
    pow_text = min(pow_text, 9)

    chain_strong = _hits(head + " " + prose, CHAIN_STRONG_RE)
    chain_weak = _hits(prose, CHAIN_WEAK_RE)
    chain_text = min(len(chain_strong), 6) + min(len(chain_weak), 2) + (1 if HEAD_CHAIN_RE.search(head) else 0)

    node_h, node_b = _hits(head, NODE_RE), _hits(prose, NODE_RE)
    node_all = node_h | node_b
    node_text = min(sum(1 + (1 if k in node_h else 0) + (1 if k == "masternode" else 0) for k in node_all), 6)

    # ---------------- TREE ----------------
    th = tree_hits(repo.paths)
    t_pow = min(3 * len(th["pow"]), 6)
    t_chain = min(sum(TREE_W.get(k, 3 if k.startswith("file:") else 2) for k in th["chain"]), 7)
    t_node = min(3 * len(th["node"]), 6)

    # ---------------- CODE ----------------
    ch_pow = _hits(code_blob, CODE_POW_RE)
    ch_chain = _hits(code_blob, CODE_CHAIN_RE)
    ch_node = _hits(code_blob, CODE_NODE_RE)
    c_pow = min(sum(CODE_POW_W[k] for k in ch_pow), 8)
    c_chain = min(sum(CODE_CHAIN_W[k] for k in ch_chain), 10)
    c_node = min(sum(CODE_NODE_W[k] for k in ch_node), 6)
    toy = bool(_hits(code_blob, CODE_TOY_RE)) and not (th["chain"] or ch_chain)

    # ---------------- NEGATIVES ----------------
    neg_h, neg_b = _hits(head, NEG_RE), _hits(prose, NEG_RE)
    neg = 4 * len(neg_h) + min(2 * len(neg_b - neg_h), 8)
    if UPSTREAM_TITLE.search(full_text[:400]) and not re.search(UPSTREAM_TITLE, name):
        neg += 3
        neg_b = neg_b | {"unmodified upstream"}
    if toy:
        neg += 6
        neg_b = neg_b | {"toy-code"}
    if t_chain + c_chain >= 6:       # real consensus code beats scary words in a README
        neg = neg // 2

    score = (pow_text + t_pow + c_pow + chain_text + t_chain + c_chain
             + node_text + t_node + c_node - neg)

    # ---------------- GATES ----------------
    pow_ev = bool(pow_all & P.POW_STRONG_KEYS) or bool(th["pow"]) or bool(ch_pow - {"code:algo"}) \
        or ("code:algo" in ch_pow and bool(th["chain"] or ch_chain)) \
        or ("proof-of-work" in pow_all and len(chain_strong) >= 4)
    real_chain = bool(th["chain"] - {"layout:bitcoin-fork"}) or bool(ch_chain) or len(chain_strong) >= 3 \
        or ("layout:bitcoin-fork" in th["chain"] and len(chain_strong) >= 1)
    node_signals = len(node_all) + (1 if th["node"] else 0) + (1 if ch_node - {"code:p2p"} else 0)
    node_ev = bool(node_all or th["node"] or "code:masternode" in ch_node) and \
        (node_signals >= 2 or "masternode" in node_all or "code:masternode" in ch_node)

    pos = bool(re.search(P.POS_RE, head + " " + prose, re.I))
    if pos and not (pow_all & {"proof-of-work", "algorithm", "mining", "miner", "pool"}):
        pow_ev = False          # PoS/PoA chain: pow.rs etc. come from the upstream client, not from PoW

    m = P.ALGO_STRONG_RE.search(head) or P.ALGO_STRONG_RE.search(prose) or P.ALGO_STRONG_RE.search(code_blob)
    repo.algo = m.group(1) if m else None

    repo.flags = {
        "pow_ev": pow_ev, "real_chain": real_chain, "node_ev": node_ev, "toy": toy,
        "hard_neg": sorted(neg_h & P.HARD_NEG_HEAD),
        "testnet_seeds": bool(re.search(r"\btestnet\b", code_blob, re.I) and ch_chain & {"code:ports", "code:magic"}),
    }
    repo.score = score
    repo.reasons = (
        [f"+pow:{x}" for x in sorted(pow_all)] + [f"+{x}" for x in sorted(th["pow"] | th["chain"] | th["node"])]
        + [f"+{x}" for x in sorted(ch_pow | ch_chain | ch_node)]
        + [f"+node:{x}" for x in sorted(node_all)] + [f"+chain:{x}" for x in sorted(chain_strong)]
        + [f"-{x}" for x in sorted(neg_h | neg_b)]
    )
    names = "\n".join(repo.paths[:400])
    repo.stage = detect_stage(head + "\n" + full_text + "\n" + code_blob[:20000] + "\n" + names, repo.flags)

    repo.kind = None
    if score < min_score or repo.flags["hard_neg"] or not real_chain:
        return
    if pow_ev:
        repo.kind = "pow"
    elif include_node and node_ev:
        repo.kind = "node"


def prelim_score(repo: Repo) -> int:
    """Cheap pre-check (README + description only). Decides whether it is worth reading the file tree."""
    name = re.sub(r"[-_.]+", " ", repo.full_name.split("/")[-1])
    head = " ".join([name, repo.description, " ".join(repo.topics)])
    prose = strip_code_fences((repo.text or "")[:60000])
    s = 0
    s += len(_hits(head + " " + prose, POW_RE)) + len(_hits(head + " " + prose, NODE_RE))
    s += len(_hits(head + " " + prose, CHAIN_STRONG_RE))
    s += 1 if HEAD_CHAIN_RE.search(head) else 0
    return s


def summary_line(repo: Repo, max_items: int = 4) -> str:
    """Short human evidence string, e.g. 'chainparams, pow.cpp, RandomX'."""
    bits = []
    th = tree_hits(repo.paths)
    for k in sorted(th["chain"] | th["pow"] | th["node"]):
        bits.append(k.split(":", 1)[-1])
    if repo.algo:
        bits.append(repo.algo)
    return ", ".join(dict.fromkeys(bits))[:120] if bits else ""
