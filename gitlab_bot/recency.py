"""
Recency: decides WHEN a project reached its current stage, using only the last N days (default 7).

There is deliberately NO rule about repository age or how old the first commits are. A years-old repository is
judged exactly like a new one. What counts is:

  1. ACTIVITY GATE   the latest commit, or the latest release / tag, is within the last N days.
  2. STAGE EVENTS    things that happened in the last N days and show a stage change:
                       - releases / tags (name + notes: testnet, pre-testnet, devnet, mainnet, launch, genesis)
                       - commit messages
                       - diffs (ADDED lines only) of chain files and docs: a new genesis hash, a new testnet config,
                         README text such as "testnet is live"
  3. MAINNET AGE     if the project is on mainnet, WHEN did it launch?
                       a. a fresh genesis hash in a recent diff            (a new / reset chain)
                       b. the earliest launch release / tag               (name "mainnet", "v1.0.0", notes "mainnet launched")
                       c. the commit that introduced the genesis / chainparams file
                       d. repository creation date, only if nothing else can be dated
                     launch <= N days ago -> ACCEPT (stage "mainnet");  older -> skip;  unknown -> skip (and retry later).

`assess(g, repo, cfg, now)` talks to GitLab; everything else is pure and unit-tested offline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import patterns as P
from gl import parse_ts

log = logging.getLogger("gl-bot")

STAGE_RANK = {"pre-testnet": 0, "testnet": 1, "mainnet": 2}

FUTURE = re.compile(
    r"main[\s-]?net[^.\n]{0,60}\b(will|planned|upcoming|soon|coming|expected|eta|tba|tbd|q[1-4]|later)\b|"
    r"\b(will|planned|upcoming|soon|prepar\w+|before|ahead of|towards?|until)\b[^.\n]{0,40}main[\s-]?net", re.I)
NOT_V1 = re.compile(r"rc|beta|alpha|test|dev|pre|snapshot", re.I)
HEX64 = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
NET_REG = re.compile(r"CRegTestParams|\bregtest\b|\bsignet\b|CBaseChainParams::REGTEST|Network::Regtest", re.I)
NET_TEST = re.compile(r"CTestNetParams|\btest[\s_-]?net\b|\bdev[\s_-]?net\b|CBaseChainParams::TESTNET|Network::Test|ChainType::Test", re.I)
NET_MAIN = re.compile(P.MAINNET_CODE.pattern, re.I)
GENESIS_FILE = re.compile(r"(^|/)genesis[\w.-]*\.(json|toml|ya?ml|rs|go|cpp|h|py)$", re.I)
CHAINPARAMS_FILE = re.compile(r"(^|/)chain_?params\w*\.(cpp|h|rs|go|py|json|toml)$", re.I)
README_NAME = re.compile(r"(^|/)readme(\.\w+)?$", re.I)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    stage: str                  # pre-testnet | testnet | mainnet | "?" (genesis of an unknown network)
    when: datetime
    kind: str                   # release | tag | commit | genesis | config | readme
    source: str                 # human text, e.g. "release v1.0.0" / "diff src/chainparams.cpp @1a2b3c4d"
    strong: bool = True         # commit messages alone are weak

    def line(self, now: datetime) -> str:
        return f"{self.stage:11s} {days_between(now, self.when):5.1f}d ago  {self.kind:7s} {self.source}"


@dataclass
class Recency:
    now: datetime
    days: float
    # activity
    last_commit: datetime | None = None
    last_mark: datetime | None = None          # newest release / tag
    last_mark_name: str = ""
    commits_in_window: int = 0
    created_recently: bool = False
    # evidence
    events: list = field(default_factory=list)
    launch_marks: list = field(default_factory=list)   # [(datetime, name, "strong"|"v1")]
    mainnet_at: datetime | None = None
    mainnet_src: str = ""
    # verdict
    text_stage: str = ""
    stage: str = ""
    accept: bool = False
    established: bool = False                   # mainnet launched long ago: never worth re-reading
    reason: str = ""
    errors: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.created_recently or self.commits_in_window > 0 or (
            self.last_mark is not None and days_between(self.now, self.last_mark) <= self.days)

    @property
    def launch_activity(self) -> bool:
        """Any real (non commit-message) network event in the window; lets a fork through the fork filter."""
        return any(e.strong for e in self.events)

    def commit_age(self) -> float | None:
        return days_between(self.now, self.last_commit) if self.last_commit else None

    def mark_age(self) -> float | None:
        return days_between(self.now, self.last_mark) if self.last_mark else None

    def mainnet_age(self) -> float | None:
        return days_between(self.now, self.mainnet_at) if self.mainnet_at else None


def days_between(now: datetime, then: datetime) -> float:
    return max(0.0, (now - then).total_seconds() / 86400)


def _fmt_age(x: float | None) -> str:
    return "unknown" if x is None else (f"{x:.1f} days ago" if x >= 0.1 else "just now")


def summary(rec: Recency) -> str:
    """One short line for Discord / logs."""
    if rec is None:
        return ""
    bits = []
    if rec.stage == "mainnet" and rec.mainnet_age() is not None:
        bits.append(f"mainnet launched {rec.mainnet_age():.1f}d ago ({rec.mainnet_src})")
    elif rec.events:
        e = max(rec.events, key=lambda e: (e.strong, e.when))
        bits.append(f"{e.stage} event {days_between(rec.now, e.when):.1f}d ago ({e.source})")
    if rec.commit_age() is not None:
        bits.append(f"last commit {rec.commit_age():.1f}d ago")
    return ", ".join(bits)


# --------------------------------------------------------------------------- #
# Pure text / diff analysis
# --------------------------------------------------------------------------- #
def text_stages(text: str, *, need_action: bool = False) -> list[str]:
    """Stages mentioned in release notes / commit messages. `need_action` (commit messages) also requires a verb."""
    out: list[str] = []
    if not text:
        return out
    if need_action and not P.EV_ACTION.search(text):
        return out
    if P.EV_PRETESTNET.search(text):
        out.append("pre-testnet")
    if P.EV_TESTNET.search(P.EV_PRETESTNET.sub(" ", text)):
        out.append("testnet")
    if is_mainnet_launch_text(text):
        out.append("mainnet")
    return out


LIVE_PAST = re.compile(r"main[\s-]?net\s+(is\s+|has\s+|was\s+)?(now\s+|officially\s+)?(live|launched|released|running|started|went live)\b|"
                       r"\b(launched|launching|released)\s+(the\s+|our\s+)?main[\s-]?net\b|\bwent live\b", re.I)
LAUNCH_NOTE = re.compile(r"main[\s-]?net[^.\n]{0,40}\b(launch\w*|go[\s-]?live|goes live|live now|now live)\b|"
                         r"\b(launch\w*|go[\s-]?live)\b[^.\n]{0,40}main[\s-]?net", re.I)


def is_mainnet_launch_text(text: str) -> bool:
    """Explicit statement that mainnet launched / launches. Plans ("will launch", "prepare for") do not count,
    and neither do generic mentions ("fix mainnet sync", "update mainnet genesis docs")."""
    if not text or not P.EV_MAINNET.search(text):
        return False
    if LIVE_PAST.search(text):
        return True
    return bool(LAUNCH_NOTE.search(text)) and not FUTURE.search(text)


def launch_mark_kind(name: str, text: str) -> str | None:
    """Is this release / tag a MAINNET LAUNCH marker?  'strong' (named mainnet / notes say so) or 'v1' (v1.0.0)."""
    if re.search(r"main[\s_-]?net", name or "", re.I) and not FUTURE.search(name):
        return "strong"
    if is_mainnet_launch_text(text):
        return "strong"
    if P.EV_V1.match(name or "") and not NOT_V1.search(name or ""):
        return "v1"
    return None


def scan_diff(diff: str) -> dict:
    """Added-line analysis of one file diff.  Returns {network: {"hashes": set, "genesis": bool, "seeds": bool}}
    where network is mainnet | testnet | ? (could not tell). Hunk context lines decide the network."""
    nets: dict[str, dict] = {}
    removed_hex: set[str] = set()
    cur = None
    for line in (diff or "").splitlines():
        if line.startswith("@@"):
            cur = None
            tail = line.split("@@", 2)[-1]
            cur = _net_of(tail, None)
            continue
        if not line or line[0] not in " +-" or line.startswith(("+++", "---")):
            continue
        sign, body = line[0], line[1:]
        cur = _net_of(body, cur)
        if sign == "-":
            removed_hex.update(h.lower().removeprefix("0x") for h in HEX64.findall(body) if P.DIFF_GENESIS.search(body))
        elif sign == "+":
            n = nets.setdefault(cur or "?", {"hashes": set(), "genesis": False, "seeds": False})
            if P.DIFF_GENESIS.search(body):
                n["genesis"] = True
                n["hashes"].update(h.lower().removeprefix("0x") for h in HEX64.findall(body))
            if P.DIFF_SEEDS.search(body):
                n["seeds"] = True
    for n in nets.values():
        n["hashes"] -= removed_hex          # a hash that merely moved is not a new genesis
    nets.pop("regtest", None)
    return nets


def _net_of(text: str, current: str | None) -> str | None:
    if NET_REG.search(text):
        return "regtest"
    if NET_TEST.search(text):
        return "testnet"
    if NET_MAIN.search(text):
        return "mainnet"
    return current


def path_network(path: str) -> str | None:
    if re.search(r"test[-_]?net|dev[-_]?net|regtest", path, re.I):
        return "testnet"
    if re.search(r"main[-_]?net", path, re.I):
        return "mainnet"
    return None


def diff_events(path: str, diff: str, new_file: bool, when: datetime, sha: str) -> list[Event]:
    """Stage events produced by ONE file of ONE commit (only added lines count)."""
    src = f"diff {path} @{sha[:8]}"
    out: list[Event] = []
    added = "\n".join(l[1:] for l in (diff or "").splitlines() if l.startswith("+") and not l.startswith("+++"))
    if P.DIFF_CODE_PATH.search(path):
        pnet = path_network(path)
        for net, info in scan_diff(diff).items():
            net = pnet or net
            if info["genesis"] and (info["hashes"] or new_file):
                out.append(Event(net, when, "genesis", src + " (new genesis hash)"))
            elif net == "testnet" and (info["seeds"] or (info["genesis"])):
                out.append(Event("testnet", when, "config", src + " (testnet config)"))
        if new_file and pnet and not any(e.kind == "genesis" for e in out):
            out.append(Event(pnet, when, "config", src + f" (new {pnet} file)"))
    if P.DIFF_DOC_PATH.search(path) and added:
        if P.EV_PRETESTNET.search(added):
            out.append(Event("pre-testnet", when, "readme", src + " (pre-testnet text added)"))
        if P.TESTNET_LIVE.search(P.EV_PRETESTNET.sub(" ", added)):
            out.append(Event("testnet", when, "readme", src + " (testnet text added)"))
        if is_mainnet_launch_text(added):        # exchange links / explorer URLs added to a mature README do not count
            out.append(Event("mainnet", when, "readme", src + " (mainnet launch text added)"))
    return out


def _date(d: dict | None, *keys: str) -> datetime | None:
    for k in keys:
        v = (d or {}).get(k)
        if v:
            t = parse_ts(v)
            if t:
                return t
    return None


def collect_marks(tags: list[dict], releases: list[dict], now: datetime) -> dict[str, dict]:
    """Merge tags and releases into {name: {name, when, text}}. Upcoming / future-dated releases are ignored."""
    marks: dict[str, dict] = {}
    for t in tags or []:
        name = t.get("name") or ""
        c = t.get("commit") or {}
        when = _date(t, "created_at") or _date(c, "committed_date", "created_at")
        text = " ".join(filter(None, [t.get("message"), c.get("title")]))
        if name and when and when <= now + timedelta(hours=1):
            marks[name] = {"name": name, "when": when, "text": text[:3000], "src": "tag"}
    for r in releases or []:
        name = r.get("tag_name") or r.get("name") or ""
        when = _date(r, "released_at", "created_at")
        if not name or not when or r.get("upcoming_release") or when > now + timedelta(hours=1):
            continue
        text = " ".join(filter(None, [r.get("name"), r.get("description")]))[:4000]
        old = marks.get(name)
        marks[name] = {"name": name, "when": when, "text": ((old or {}).get("text", "") + " " + text).strip(),
                       "src": "release"}
    return marks


# --------------------------------------------------------------------------- #
# The decision (pure)
# --------------------------------------------------------------------------- #
def decide(rec: Recency, text_stage: str, *, old_repo: bool, require_event: bool,
           first_file: tuple[datetime, str] | None, created: datetime | None) -> None:
    """Fill rec.stage / rec.accept / rec.reason / rec.mainnet_at from rec.events, rec.launch_marks and the text stage."""
    now, days = rec.now, rec.days
    rec.text_stage = text_stage
    cut = now - timedelta(days=days)
    # "?" = genesis of an unknown network: it is the mainnet if the project already claims mainnet, else a testnet
    for e in rec.events:
        if e.stage == "?":
            e.stage = "mainnet" if text_stage == "mainnet" else "testnet"

    if not rec.active:
        rec.stage, rec.accept = text_stage, False
        rec.reason = f"no commit and no release/tag in the last {days:g} days"
        return

    marks_strong = sorted(m for m in rec.launch_marks if m[2] == "strong")
    marks_v1 = sorted(m for m in rec.launch_marks if m[2] == "v1")
    fresh_genesis = [e for e in rec.events if e.stage == "mainnet" and e.kind in ("genesis", "config")]
    other_main = [e for e in rec.events if e.stage == "mainnet" and e.kind not in ("genesis", "config") and e.strong]
    claim = (text_stage == "mainnet" or bool(marks_strong) or bool(fresh_genesis) or bool(other_main)
             or any(m[0] >= cut for m in marks_v1) and text_stage != "testnet")

    if claim:
        rec.stage = "mainnet"
        date, src = None, ""
        old_marks = [m for m in marks_strong + (marks_v1 if text_stage == "mainnet" else []) if m[0] < cut]
        if fresh_genesis:                                    # a new genesis is a (re)launch, whatever older tags say
            e = min(fresh_genesis, key=lambda e: e.when)
            date, src = e.when, e.source
        elif old_marks:                                      # launched long ago; later notes only repeat the word
            m = min(old_marks)
            date, src = m[0], f"tag/release {m[1]}"
        else:
            cands = [(m[0], f"tag/release {m[1]}") for m in marks_strong]
            cands += [(m[0], f"tag/release {m[1]}") for m in marks_v1 if text_stage == "mainnet" or m[0] >= cut]
            cands += [(e.when, e.source) for e in other_main]
            if cands:
                date, src = min(cands)
            elif first_file:
                date, src = first_file[0], first_file[1]
            elif created:
                date, src = created, "repository created (no better date)"
        rec.mainnet_at, rec.mainnet_src = date, src
        age = rec.mainnet_age()
        if age is None:
            rec.accept, rec.reason = False, "mainnet is live but its launch date could not be determined"
        elif age <= days:
            rec.accept = True
            rec.reason = f"mainnet launched {age:.1f} days ago via {src} (<= {days:g})"
        else:
            rec.accept, rec.established = False, True
            rec.reason = f"mainnet launched {age:.1f} days ago via {src} (> {days:g} days): too old"
            hard = bool(marks_strong or fresh_genesis or other_main)
            fresh_test = [e for e in rec.events if e.stage in ("pre-testnet", "testnet") and e.strong]
            if fresh_test and not hard:
                # the README only *claims* mainnet (website / listing links) but a new testnet event just happened
                e = max(fresh_test, key=lambda e: (STAGE_RANK[e.stage], e.when))
                rec.stage, rec.accept, rec.established = e.stage, True, False
                rec.reason = (f"{e.stage}: {e.kind} event {days_between(now, e.when):.1f} days ago ({e.source}); "
                              f"README claims mainnet but no mainnet launch is dated inside the window")
        return

    # pre-testnet / testnet: text stage, upgraded by recent events
    stage = text_stage if text_stage in STAGE_RANK else "pre-testnet"
    for e in rec.events:
        if STAGE_RANK.get(e.stage, -1) > STAGE_RANK[stage] and e.stage != "mainnet":
            stage = e.stage
    rec.stage = stage
    staged = [e for e in rec.events if e.stage in ("pre-testnet", "testnet")]
    strong = [e for e in staged if e.strong]
    if old_repo and require_event and not strong:
        rec.accept = False
        hint = " (only commit-message hints)" if staged else ""
        rec.reason = (f"{stage}, but no testnet/genesis/launch event in the last {days:g} days{hint} "
                      f"(activity alone is not a stage change; REQUIRE_EVENT=false to allow)")
    else:
        rec.accept = True
        if staged:
            e = max(staged, key=lambda e: (e.strong, e.when))
            rec.reason = f"{stage}: {e.kind} event {days_between(now, e.when):.1f} days ago ({e.source})"
        else:
            rec.reason = f"{stage}: new project with activity in the last {days:g} days"


# --------------------------------------------------------------------------- #
# Network part
# --------------------------------------------------------------------------- #
def _commit_date(c: dict) -> datetime | None:
    return _date(c, "committed_date", "created_at", "authored_date")


def interesting_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """(chain files, doc files) whose recent history is worth reading."""
    blobs = [p for p in paths if not p.endswith("/") and not P.IGNORED_PATH.search(p)]
    code = [p for p in blobs if P.DIFF_CODE_PATH.search(p)]
    code.sort(key=lambda p: (not CHAINPARAMS_FILE.search(p), not GENESIS_FILE.search(p), len(p)))
    docs = [p for p in blobs if README_NAME.search(p)] + [p for p in blobs if P.DIFF_DOC_PATH.search(p)
                                                          and not README_NAME.search(p)]
    return code[:4], list(dict.fromkeys(docs))[:2]


def assess(g, repo, cfg: dict, now: datetime, *, want_latest: bool = False) -> Recency:
    """Run the full recency check for a project that already looks like a PoW / node project.
    Raises gl.Throttled / gl.Unreadable on API trouble (the caller retries later)."""
    days = float(cfg.get("recent_days", 7))
    cut = now - timedelta(days=days)
    rec = Recency(now=now, days=days)
    created = parse_ts(repo.created_at)
    rec.created_recently = bool(created and created >= cut)
    ref = repo.default_branch

    # ---- 1. activity: commits / tags / releases (3 requests) ----
    commits = g.commits(repo.id, ref, since=cut, per_page=int(cfg.get("recent_commits", 20)))
    tags = g.tags(repo.id, per_page=50)
    releases = g.releases(repo.id, per_page=50)
    rec.commits_in_window = len(commits)
    dates = [d for d in (_commit_date(c) for c in commits) if d]
    rec.last_commit = max(dates) if dates else None
    if want_latest and not commits:                       # only for --explain: show how stale it is
        latest = g.commits(repo.id, ref, per_page=1)
        rec.last_commit = _commit_date(latest[0]) if latest else None
    marks = collect_marks(tags, releases, now)
    if marks:
        newest = max(marks.values(), key=lambda m: m["when"])
        rec.last_mark, rec.last_mark_name = newest["when"], newest["name"]

    text_stage = repo.stage
    old_repo = not rec.created_recently
    if not rec.active:
        decide(rec, text_stage, old_repo=old_repo, require_event=False, first_file=None, created=created)
        return rec

    # ---- 2. events from releases / tags / commit messages ----
    for m in marks.values():
        kind = launch_mark_kind(m["name"], m["text"])
        if kind:
            rec.launch_marks.append((m["when"], m["name"], kind))
        if m["when"] < cut:
            continue
        stages = text_stages(m["name"] + " " + m["text"])
        if kind == "strong" and "mainnet" not in stages:
            stages.append("mainnet")
        if kind == "v1" and text_stage != "testnet" and "mainnet" not in stages:
            stages.append("mainnet")                       # a fresh v1.0.0 is a launch tag unless the project says testnet
        for st in stages:
            rec.events.append(Event(st, m["when"], m["src"], f"{m['src']} {m['name']}"))
    for c in commits:
        when = _commit_date(c)
        msg = (c.get("title") or "") + " " + (c.get("message") or "")
        if when:
            for st in text_stages(msg[:600], need_action=True):
                rec.events.append(Event(st, when, "commit", f"commit {c.get('short_id') or c['id'][:8]}: "
                                        f"{(c.get('title') or '')[:60]!r}", strong=False))

    # ---- 3. diffs of chain / doc files touched in the window ----
    code_files, doc_files = interesting_paths(repo.paths)
    shas: dict[str, tuple[datetime, dict]] = {}
    for p in code_files + doc_files:
        for c in g.commits(repo.id, ref, since=cut, path=p, per_page=5):
            d = _commit_date(c)
            if d and c["id"] not in shas:
                shas[c["id"]] = (d, c)
    for sha, (when, c) in sorted(shas.items(), key=lambda kv: kv[1][0], reverse=True)[:int(cfg.get("diff_commits", 8))]:
        for f in g.commit_diff(repo.id, sha):
            path = f.get("new_path") or f.get("old_path") or ""
            if P.IGNORED_PATH.search(path):
                continue
            rec.events.extend(diff_events(path, f.get("diff") or "", bool(f.get("new_file")), when, sha))

    # ---- 4. dating a mainnet launch from the file history (only needed when a mainnet is claimed) ----
    first_file = None
    needs_date = text_stage == "mainnet" or any(e.stage == "mainnet" for e in rec.events) or rec.launch_marks
    has_dated = any(e.stage == "mainnet" and e.kind in ("genesis", "config") for e in rec.events) or \
        any(m[2] == "strong" for m in rec.launch_marks)
    if needs_date and not has_dated:
        blobs = [p for p in repo.paths if not p.endswith("/") and not P.IGNORED_PATH.search(p)]
        target = next((p for p in blobs if GENESIS_FILE.search(p)), None) or \
            next((p for p in blobs if CHAINPARAMS_FILE.search(p)), None)
        if target:
            c = g.first_commit(repo.id, ref, target)
            d = _commit_date(c) if c else None
            if d:
                first_file = (d, f"{target} introduced")
    decide(rec, text_stage, old_repo=old_repo, require_event=bool(cfg.get("require_event", True)),
           first_file=first_file, created=created)
    return rec


def explain_lines(rec: Recency, cfg: dict) -> list[str]:
    """Human-readable breakdown for --explain."""
    now = rec.now
    out = [f"  recency window: {rec.days:g} days   (no repository-age rule; only latest commit / release / tag matter)"]
    out.append(f"  latest commit : {_fmt_age(rec.commit_age())}"
               + (f"   ({rec.commits_in_window} in window)" if rec.commits_in_window else ""))
    out.append(f"  latest release/tag: {_fmt_age(rec.mark_age())}" + (f"  [{rec.last_mark_name}]" if rec.last_mark_name else ""))
    out.append(f"  active in window: {rec.active}" + ("  (repository was created in the window)" if rec.created_recently else ""))
    out.append(f"  stage from text: {rec.text_stage or '-'}   detected stage: {rec.stage or '-'}")
    if rec.launch_marks:
        out.append("  launch tags/releases: " + ", ".join(
            f"{n} ({days_between(now, d):.0f}d, {k})" for d, n, k in sorted(rec.launch_marks)[:6]))
    if rec.stage == "mainnet" or rec.mainnet_at:
        out.append(f"  mainnet launch: {_fmt_age(rec.mainnet_age())}"
                   + (f"  via {rec.mainnet_src}" if rec.mainnet_src else "")
                   + (f"  [{rec.mainnet_at:%Y-%m-%d}]" if rec.mainnet_at else ""))
    if rec.events:
        out.append(f"  stage events in the last {rec.days:g} days:")
        out.extend("    " + e.line(now) for e in sorted(rec.events, key=lambda e: e.when, reverse=True)[:12])
    else:
        out.append(f"  stage events in the last {rec.days:g} days: none found")
    out.append(f"  recency verdict: {'PASS' if rec.accept else 'FAIL'} -- {rec.reason}")
    return out
