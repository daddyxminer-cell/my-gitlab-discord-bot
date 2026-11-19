#!/usr/bin/env python3
"""
GitLab -> Discord notifier for Proof-of-Work coins and node-based projects that REACHED pre-testnet, testnet
or a mainnet launch within the last RECENT_DAYS (default 7) -- however old the repository is.

v3 changes:
  * Two discovery streams: (1) newly CREATED projects, (2) recently ACTIVE projects found with keyword searches
    (testnet, mainnet, pre-testnet, genesis, pow, randomx, masternode, validator) ordered by last_activity_at.
    Project ids are de-duplicated across all queries.
  * No repository-age / "old history" rule. A project passes when its latest commit or latest release/tag is within
    RECENT_DAYS AND a stage event is found in that window (see recency.py).
  * Mainnet projects are no longer discarded: the launch is dated (genesis / chainparams history, release & tag
    names, release notes). Launch <= RECENT_DAYS ago -> posted, older -> skipped.
  * State is keyed by project id + stage (12345_testnet, 12345_mainnet): a project posted at testnet is posted
    again when it launches mainnet.

v2 changes (why it finds more, and finds the right things):
  * It no longer relies on GitLab's name/description search. It walks EVERY newly created public project
    and reads what is inside: file list, README, docs and key source files (chainparams.cpp, pow.cpp,
    genesis files, Cargo.toml ...). A coin with an empty description is found just the same.
  * Stage filter: posts pre-testnet and testnet projects, skips projects whose mainnet is already live.
  * Self-healing: retries with backoff, persistent retry queue, falls back to anonymous access if the token
    is rejected, never advances its cursor past work it could not finish, optional failure alerts.
  * Optional AI judge for borderline cases (LLM_API_KEY).

Usage:
    python bot.py --once                    # check one time (cron / GitHub Actions)
    python bot.py                           # run forever, every CHECK_INTERVAL_MINUTES
    python bot.py --once --dry-run          # print matches, send nothing, save nothing
    python bot.py --explain GROUP/PROJECT   # show exactly why a project would / would not be posted
    python bot.py --near-misses             # list projects that almost matched (to tune the bot)

Settings: see README.md / .env.example
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import llm
import patterns as P
import recency
from classify import Repo, classify, summary_line
from gl import GitLab, Throttled, Unreadable, parse_ts
from reader import read_project

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state" / "seen.json"
BACKUP_FILE = HERE / "state" / "seen.json.bak"
LOCK_FILE = HERE / "state" / "bot.lock"
STATE_VERSION = 3
FROZEN = "*"            # marker: never re-evaluate this project
MAX_RECHECKS = 6        # a NON-chain project that keeps changing is re-read at most this many times
STAGES_ALL = ("pre-testnet", "testnet", "mainnet")
MAX_QUEUE_TRIES = 8
OVERLAP = timedelta(hours=3)   # re-look at this much of the previous window (cheap insurance)

log = logging.getLogger("gl-bot")


# --------------------------------------------------------------------------- #
# State (memory)
# --------------------------------------------------------------------------- #
class StateError(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "seen": {},            # project id -> last_activity_at when last read ("*" = never re-check)
        "checks": {},          # project id -> how many times it was read
        "notified": {},        # "<id>_<stage>" -> {id, name, url, stage, at}   (permanent; one entry per project AND stage)
        "notified_names": [],  # "<lower-case group/name>_<stage>" of every posted project (permanent)
        "watch": {},           # project id -> {at}: chain-like projects, re-read on new activity at most every WATCH_HOURS
        "owners": {},          # lower-case owner -> last time one of their projects was posted
        "queue": {},           # project id -> {tries, first}  (could not be read yet; retried every run)
        "cursor": {},          # {last_ok: iso, floor_id: int, floor_for: iso}
        "health": {},          # {fail_streak, last_alert, last_digest, last_match}
        "near_miss": [],       # [{name,url,score,why,at}] latest near misses
    }


def _read_state_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "notified" not in data:
        raise ValueError("not a bot state file")
    return data


def nkey(pid, stage: str) -> str:
    return f"{pid}_{stage}"


def migrate(state: dict) -> dict:
    """Bring an older state file (v1/v2: notified keyed by project id only) to v3 (keyed by id + stage)."""
    notified = state.get("notified") or {}
    old = [k for k in notified if "_" not in str(k)]
    stage_of_name: dict[str, str] = {}
    for k in old:
        v = notified.pop(k)
        st = v.get("stage") if v.get("stage") in STAGES_ALL else "testnet"
        v.setdefault("id", int(k) if str(k).isdigit() else k)
        notified[nkey(k, st)] = v
        if v.get("name"):
            stage_of_name[v["name"].lower()] = st
    state["notified"] = notified
    state["notified_names"] = [n if n.rsplit("_", 1)[-1] in STAGES_ALL else f"{n}_{stage_of_name.get(n, 'testnet')}"
                               for n in state.get("notified_names") or []]
    if old:
        log.info("State migrated to v%d: %d posted projects now tracked per stage", STATE_VERSION, len(old))
    state.setdefault("watch", {})
    return state


def load_state() -> dict:
    damaged = []
    for path in (STATE_FILE, BACKUP_FILE):
        if not path.exists():
            continue
        try:
            data = _read_state_file(path)
        except (ValueError, OSError, UnicodeDecodeError) as e:
            log.warning("Could not read %s: %s", path.name, e)
            damaged.append(path.name)
            continue
        if path == BACKUP_FILE:
            log.warning("Main state file was damaged -- using backup")
        state = _empty_state()
        state.update(data)
        return migrate(state)
    if damaged:
        raise StateError(
            f"State file(s) {', '.join(damaged)} are damaged. Refusing to start fresh because "
            "that could re-post old projects. Delete the files in gitlab_bot/state/ to reset.")
    return _empty_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    if len(state["seen"]) > 40000:  # only "seen" is trimmed; old projects fall out of the window anyway
        keep = sorted(state["seen"], key=int)[-40000:]
        state["seen"] = {k: state["seen"][k] for k in keep}
        state["checks"] = {k: v for k, v in state["checks"].items() if k in state["seen"]}
    state["notified_names"] = sorted(set(state["notified_names"]))
    state["near_miss"] = state["near_miss"][-60:]
    if len(state["watch"]) > 5000:
        keep = sorted(state["watch"], key=lambda k: state["watch"][k].get("at", ""))[-5000:]
        state["watch"] = {k: state["watch"][k] for k in keep}
    data = json.dumps(state, indent=1, ensure_ascii=False)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if STATE_FILE.exists():
        try:
            _read_state_file(STATE_FILE)
            shutil.copy2(STATE_FILE, BACKUP_FILE)
        except (ValueError, OSError, UnicodeDecodeError):
            pass
    os.replace(tmp, STATE_FILE)


def merge_state_files(other_path: Path) -> None:
    """Union another copy of the state into ours (used by the GitHub Actions workflow)."""
    if not other_path.exists():
        return
    try:
        other = migrate(_read_state_file(other_path))
    except (ValueError, OSError, UnicodeDecodeError) as e:
        log.warning("Ignoring unreadable state copy %s: %s", other_path, e)
        return
    try:
        state = load_state()
    except StateError:
        state = _empty_state()
    for k, v in (other.get("seen") or {}).items():
        if state["seen"].get(k) != FROZEN:
            state["seen"][k] = FROZEN if v == FROZEN else state["seen"].get(k, v)
    notified = dict(other.get("notified") or {})
    notified.update(state["notified"])
    state["notified"] = notified
    state["notified_names"] = list(set(state["notified_names"]) | set(other.get("notified_names", [])))
    for o, t in (other.get("owners") or {}).items():
        state["owners"][o] = max(t, state["owners"].get(o, ""))
    for k, v in (other.get("queue") or {}).items():
        state["queue"].setdefault(k, v)
    for k, v in (other.get("watch") or {}).items():
        if v.get("at", "") > state["watch"].get(k, {}).get("at", ""):
            state["watch"][k] = v
    for k, v in (other.get("checks") or {}).items():
        state["checks"][k] = max(int(v), int(state["checks"].get(k, 0)))
    oc, sc = other.get("cursor") or {}, state["cursor"]
    if oc.get("last_ok", "") > sc.get("last_ok", ""):
        state["cursor"] = oc
    state["initialized"] = bool(state["initialized"] or other.get("initialized"))
    save_state(state)
    log.info("Merged state from %s", other_path)


class SingleInstance:
    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK_FILE, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise AlreadyRunning(
                "Another copy of the GitLab bot is already running (state/bot.lock). "
                "Run only one copy, otherwise projects get posted once per copy.") from None
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
STAGE_LABEL = {"pre-testnet": "pre-testnet", "testnet": "testnet", "mainnet": "mainnet"}


def build_message(repo: Repo, show_details: bool) -> str:
    msg = f"New record added: {repo.url}"
    if show_details:
        bits = ["PoW coin" if repo.kind == "pow" else "Node project", STAGE_LABEL.get(repo.stage, repo.stage)]
        if repo.algo:
            bits.append(repo.algo)
        msg += "\n-# " + " · ".join(bits)
        ev = summary_line(repo)
        if ev:
            msg += "\n-# found: " + ev
        rs = recency.summary(repo.recency) if repo.recency else ""
        if rs:
            msg += "\n-# when: " + rs
    return msg


def post_webhook(webhook: str, content: str, username: str | None = None) -> None:
    payload = {"content": content[:1900], "allowed_mentions": {"parse": []}}  # nothing can ping @everyone
    if username:
        payload["username"] = username
    for _ in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        break
    time.sleep(1.2)  # stay under Discord's webhook rate limit


def alert(cfg: dict, text: str) -> None:
    """Operational message to the (optional) alert webhook. Never raises."""
    hook = cfg.get("alert_webhook")
    log.warning("ALERT: %s", text)
    if not hook or cfg.get("dry_run"):
        return
    try:
        post_webhook(hook, f"[gitlab-bot] {text}", cfg.get("username"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not send alert: %s", e)


# --------------------------------------------------------------------------- #
# Reading + judging one project
# --------------------------------------------------------------------------- #
def posted_rank(state: dict, repo: Repo) -> int:
    """Highest stage this project was already posted at (-1 = never). Keyed by id + stage, name + stage as backup."""
    rank = -1
    for st in STAGES_ALL:
        if nkey(repo.id, st) in state["notified"] or f"{repo.full_name.lower()}_{st}" in state["notified_names"]:
            rank = max(rank, recency.STAGE_RANK[st])
    return rank


def hard_block(state: dict, repo: Repo, cfg: dict) -> str | None:
    """Reasons that never change: never look at this project again."""
    if repo.owner.lower() in cfg["block_owners"]:
        return "owner blocked"
    if posted_rank(state, repo) >= recency.STAGE_RANK["mainnet"]:
        return "already posted at mainnet (last stage)"
    return None


def blocked_reason(state: dict, repo: Repo, cfg: dict, now: datetime, stage: str | None = None) -> str | None:
    """Why this project must not be posted now (None = ok). With `stage`, also blocks a stage that was already posted
    (or a lower one); a project that was posted before may move to a HIGHER stage without the owner cool-down."""
    hb = hard_block(state, repo, cfg)
    if hb:
        return hb
    rank = posted_rank(state, repo)
    if stage and rank >= recency.STAGE_RANK.get(stage, 0):
        return f"already posted at {stage} (or later)"
    days = cfg["one_per_owner_days"]
    last = state["owners"].get(repo.owner.lower())
    if rank < 0 and days > 0 and last and datetime.fromisoformat(last) > now - timedelta(days=days):
        return f"owner {repo.owner} already posted in the last {days:g} days"
    return None


def evaluate(g: GitLab, repo: Repo, cfg: dict, now: datetime | None = None, force_recency: bool = False) -> Repo:
    """Read the project and set its verdict. Raises Throttled / Unreadable on API trouble.
    force_recency: also run the recency check for projects that did not score (used by --explain)."""
    now = now or datetime.now(timezone.utc)
    read_project(g, repo)
    if repo.skip_reason:
        repo.kind = None
        return repo
    classify(repo, cfg["min_score"], cfg["include_node"])

    # Borderline -> optional AI judge (cannot break anything: failures keep the rule verdict)
    near = cfg["min_score"] - 3 <= repo.score <= cfg["min_score"] + 3
    if cfg["use_llm"] and near and not repo.flags.get("hard_neg") and (repo.flags.get("real_chain") or repo.flags.get("pow_ev")):
        v = llm.judge(repo)
        repo.llm = v
        if v:
            c = v["confidence"]
            if v.get("wanted") and c >= 0.65 and v.get("kind") in ("pow", "node") and not repo.kind:
                repo.kind = v["kind"] if (v["kind"] == "pow" or cfg["include_node"]) else None
                repo.reasons.append(f"+ai:{v.get('reason', '')[:60]}")
                if v.get("stage") in ("pre-testnet", "testnet"):
                    repo.stage = v["stage"]
            elif not v.get("wanted") and c >= 0.75 and repo.kind:
                repo.reasons.append(f"-ai:{v.get('reason', '')[:60]}")
                repo.kind = None
            if v.get("algorithm") and not repo.algo:
                repo.algo = str(v["algorithm"])[:30]

    # Recency: latest commit / release / tag within RECENT_DAYS + a stage event inside that window.
    # (No repository-age rule: a years-old repository is judged like a new one.)
    if (repo.kind or force_recency) and cfg["recent_days"] > 0 and not repo.skip_reason:
        rec = recency.assess(g, repo, cfg, now, want_latest=force_recency)
        repo.recency = rec
        if repo.kind:
            repo.stage = rec.stage or repo.stage
            if not rec.accept:
                repo.reasons.append("-recency")
                repo.skip_reason = rec.reason
                repo.kind = None

    if repo.kind and repo.stage not in cfg["stages"]:
        repo.reasons.append(f"-stage:{repo.stage}")
        repo.skip_reason = f"stage {repo.stage} is not in STAGES ({', '.join(cfg['stages'])})"
        repo.kind = None
    return repo


def confirm_not_fork(g: GitLab, repo: Repo) -> bool:
    """True if the project is original. Mirrors are always rejected. A FORK is accepted only when it shows a real
    recent network launch of its own (new genesis / testnet / mainnet event); an unmodified fork is noise."""
    d = g.project(repo.id)
    if d is None:
        return False
    if d.get("mirror") or d.get("import_status") == "mirror":
        return False
    if d.get("forked_from_project"):
        rec = repo.recency
        return bool(rec is not None and rec.accept and rec.launch_activity)
    return True


# --------------------------------------------------------------------------- #
# Main check
# --------------------------------------------------------------------------- #
def list_candidates(g: GitLab, state: dict, cfg: dict, now: datetime) -> tuple[list[Repo], bool]:
    """Newly created projects with recent activity. Returns (repos, listing_complete)."""
    last_ok = parse_ts(state["cursor"].get("last_ok"))
    window_h = cfg["max_age_hours"] if last_ok else min(cfg["max_age_hours"], cfg["first_run_hours"])
    created_cut = now - timedelta(hours=window_h)
    since = max(created_cut, (last_ok - OVERLAP) if last_ok else created_cut)

    cur = state["cursor"]
    floor_for = parse_ts(cur.get("floor_for"))
    if not cur.get("floor_id") or not floor_for or created_cut - floor_for > timedelta(hours=12):
        cur["floor_id"] = g.find_window_id(created_cut)
        cur["floor_for"] = created_cut.isoformat(timespec="seconds")
        log.info("Project id window starts at id %s", cur["floor_id"])

    found: dict[int, Repo] = {}
    complete = True
    try:
        for page in g.list_new_projects(cur["floor_id"], since):
            for d in page:
                created = parse_ts(d.get("created_at"))
                if created and created < created_cut:
                    continue
                found.setdefault(int(d["id"]), Repo.from_api(d))
            if cfg["max_projects"] and len(found) >= cfg["max_projects"]:
                log.warning("Reached MAX_PROJECTS=%d; the rest is read next run", cfg["max_projects"])
                complete = False
                break
    except (Throttled, Unreadable) as e:
        log.warning("Project listing stopped early: %s (%d projects so far)", e, len(found))
        complete = False
    log.info("Listed %d new projects with activity since %s (complete=%s)", len(found),
             since.strftime("%m-%d %H:%M"), complete)
    n_new = len(found)
    if cfg["active_stream"] and cfg["recent_days"] > 0:
        if not list_active(g, state, cfg, now, found):
            complete = False
        log.info("Active stream: +%d projects (%d total after de-duplication, %d from the created stream)",
                 len(found) - n_new, len(found), n_new)
    return list(found.values()), complete


def list_active(g: GitLab, state: dict, cfg: dict, now: datetime, found: dict[int, Repo]) -> bool:
    """Second stream: public projects with recent ACTIVITY (old repositories included), found with one keyword
    query each, ordered by last_activity_at. Adds to `found` (de-duplicated by project id).
    Returns False when some query failed (the active cursor then does not advance, so nothing is lost)."""
    cur = state["cursor"]
    window = now - timedelta(days=cfg["recent_days"])
    last = parse_ts(cur.get("active_ok"))
    since = max(window, last - OVERLAP) if last else window
    cap = cfg["active_max_pages"]
    ok = True
    for kw in cfg["active_keywords"]:
        n = added = 0
        try:
            for page in g.list_active(kw, since, max_pages=cap):
                for d in page:
                    n += 1
                    pid = int(d["id"])
                    if pid not in found:
                        r = Repo.from_api(d)
                        r.origin = "active"
                        found[pid] = r
                        added += 1
            if n >= cap * 100:
                log.warning("Active query %r hit the %d-page cap (%d projects); older activity in the window is not listed",
                            kw, cap, n)
        except Throttled as e:
            log.warning("Active stream stopped (%s); %d hits so far", e, len(found))
            return False
        except Unreadable as e:
            log.warning("Active query %r failed: %s", kw, e)
            ok = False
        log.info("  active %-12s %4d hits, %4d new after de-duplication", kw, n, added)
    return ok


def _remember(state: dict, r: Repo, now: datetime, silent: bool = False) -> None:
    """Record a project as posted AT ITS STAGE (id + stage). It stays re-checkable until its last stage (mainnet)."""
    stamp = now.isoformat(timespec="seconds")
    entry = {"id": r.id, "name": r.full_name, "url": r.url, "kind": r.kind, "stage": r.stage,
             "score": r.score, "at": stamp}
    if silent:
        entry["silent"] = True
    state["notified"][nkey(r.id, r.stage)] = entry
    state["notified_names"] = list(set(state["notified_names"]) | {f"{r.full_name.lower()}_{r.stage}"})
    state["owners"][r.owner.lower()] = stamp
    state["seen"][str(r.id)] = FROZEN if r.stage == "mainnet" else r.pushed_at
    state["watch"][str(r.id)] = {"at": stamp}
    state["queue"].pop(str(r.id), None)


def check_once(cfg: dict, g: GitLab, dry_run: bool = False, only_ids: set[int] | None = None) -> int:
    now = datetime.now(timezone.utc)
    cfg["dry_run"] = dry_run
    state = load_state()
    health = state["health"]
    try:
        repos, complete = list_candidates(g, state, cfg, now)
    except (Throttled, Unreadable) as e:
        health["fail_streak"] = health.get("fail_streak", 0) + 1
        log.error("Could not list projects: %s", e)
        if health["fail_streak"] >= 3:
            _maybe_alert(cfg, state, f"GitLab unreachable or rate limited for {health['fail_streak']} runs in a row: {e}")
        if not dry_run:
            save_state(state)
        return 0

    # --- add the retry queue (projects we could not read last time) ---
    ids = {r.id for r in repos}
    for sid in list(state["queue"]):
        if int(sid) in ids:
            continue
        try:
            d = g.project(sid)
        except (Throttled, Unreadable):
            continue
        if d is None or not d.get("default_branch"):
            state["queue"].pop(sid, None)
            continue
        repos.append(Repo.from_api(d))
    if only_ids is not None:
        repos = [r for r in repos if r.id in only_ids]

    first_run = not state["initialized"] and not cfg["post_on_first_run"]
    if first_run and complete:
        log.info("First run / no saved state: starting from NOW. Nothing is posted for projects that already exist; "
                 "everything created from now on is checked. (Use --discover 30 to look back.)")
        if not dry_run:
            state["cursor"]["last_ok"] = now.isoformat(timespec="seconds")
            state["initialized"] = True
            save_state(state)
        return 0
    todo: list[Repo] = []
    for repo in sorted(repos, key=lambda r: r.created_at):
        sid = str(repo.id)
        prev = state["seen"].get(sid)
        queued = sid in state["queue"]
        if not queued and (prev == FROZEN or prev == repo.pushed_at):
            continue
        if repo.archived or repo.stars < cfg["min_stars"] or not repo.default_branch:
            if not repo.default_branch and not queued:
                state["seen"][sid] = repo.pushed_at   # empty now; re-read if it gets its first push
            else:
                state["seen"][sid] = FROZEN
            continue
        if hard_block(state, repo, cfg):
            state["seen"][sid] = FROZEN
            continue
        w = state["watch"].get(sid)
        if w:   # chain-like project: re-read on new activity, but not more often than WATCH_HOURS
            at = parse_ts(w.get("at"))
            if at and now - at < timedelta(hours=cfg["watch_hours"]) and not queued:
                continue
        elif state["checks"].get(sid, 0) >= MAX_RECHECKS:
            state["seen"][sid] = FROZEN
            continue
        todo.append(repo)

    log.info("Reading %d projects (%d workers)...", len(todo), cfg["workers"])
    matches: list[Repo] = []
    seen_updates: dict[str, str] = {}
    unreadable: set[str] = set()
    near: list[dict] = []
    stop = threading.Event()
    t0 = time.time()
    throttled_msg: list[str] = []
    lock = threading.Lock()

    budget_hit: list[bool] = []

    def work(repo: Repo):
        if cfg["budget_s"] and time.time() - t0 > cfg["budget_s"]:
            budget_hit.append(True)
            stop.set()
        if stop.is_set():
            return repo, "stopped"
        try:
            evaluate(g, repo, cfg, now)
            if repo.kind and not confirm_not_fork(g, repo):
                repo.kind, repo.skip_reason = None, "fork / mirror"
            return repo, "ok"
        except Throttled as e:
            with lock:
                throttled_msg.append(str(e))
            stop.set()
            return repo, "throttled"
        except Unreadable as e:
            log.debug("unreadable %s: %s", repo.full_name, e)
            return repo, "unreadable"
        except Exception as e:  # noqa: BLE001 - one bad project must never stop the run
            log.warning("Error reading %s: %s: %s", repo.full_name, type(e).__name__, e)
            return repo, "unreadable"

    done = 0
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = [pool.submit(work, r) for r in todo]
        for fut in as_completed(futures):
            repo, status = fut.result()
            sid = str(repo.id)
            done += 1
            if done % 250 == 0:
                log.info("  ... %d/%d read (%.0fs)", done, len(todo), time.time() - t0)
            if status == "stopped":
                continue                              # not read: stays unseen -> listed again next run
            if status == "throttled" or status == "unreadable":
                unreadable.add(sid)
                continue
            chain_like = repo.recency is not None or (repo.flags.get("real_chain") and (
                repo.flags.get("pow_ev") or repo.flags.get("node_ev")) and not repo.flags.get("hard_neg"))
            if chain_like:
                state["watch"][sid] = {"at": now.isoformat(timespec="seconds")}   # re-read on new activity, throttled
            else:
                state["checks"][sid] = state["checks"].get(sid, 0) + 1
            if repo.kind:
                matches.append(repo)
                log.info("MATCH %s %s score=%d [%s] %s", repo.kind.upper(), repo.stage, repo.score,
                         repo.algo or "-", repo.url)
            else:
                log.debug("no  score=%3d %s %s %s", repo.score, repo.full_name, repo.skip_reason or "",
                          " ".join(repo.reasons[:12]))
                seen_updates[sid] = repo.pushed_at
                if (repo.score >= cfg["min_score"] - 4 and not repo.flags.get("hard_neg")
                        and (repo.flags.get("real_chain") or repo.flags.get("pow_ev")) and not repo.skip_reason):
                    near.append({"name": repo.full_name, "url": repo.url, "score": repo.score,
                                 "stage": repo.stage, "why": " ".join(repo.reasons[:10]),
                                 "at": now.isoformat(timespec="seconds")})

    # ---- bookkeeping ----
    if throttled_msg:
        log.warning("%s -- stopped early, unread projects are retried next run.", throttled_msg[0])
    for sid in unreadable:
        q = state["queue"].setdefault(sid, {"tries": 0, "first": now.isoformat(timespec="seconds")})
        q["tries"] += 1
        if q["tries"] >= MAX_QUEUE_TRIES:
            log.info("Giving up on project %s after %d tries", sid, q["tries"])
            state["queue"].pop(sid, None)
            seen_updates[sid] = FROZEN
    for sid, v in seen_updates.items():
        state["seen"][sid] = v
        state["queue"].pop(sid, None)

    # one match per owner per run
    to_send: list[Repo] = []
    for repo in sorted(matches, key=lambda r: -r.score):
        reason = blocked_reason(state, repo, cfg, now, stage=repo.stage)
        same_owner = (cfg["one_per_owner_days"] > 0 and posted_rank(state, repo) < 0
                      and any(r.owner.lower() == repo.owner.lower() and posted_rank(state, r) < 0 for r in to_send))
        if reason or same_owner:
            log.info("Skip (%s): %s [%s]", reason or "same owner this run", repo.full_name, repo.stage)
            # not FROZEN: the project may reach its next stage later (testnet -> mainnet)
            state["seen"][str(repo.id)] = FROZEN if hard_block(state, repo, cfg) else repo.pushed_at
            state["queue"].pop(str(repo.id), None)
            continue
        to_send.append(repo)

    if first_run:
        log.info("First run / no saved state: remembering %d current matches, posting nothing. "
                 "From now on only brand-new projects are posted.", len(to_send))
        for r in to_send:
            _remember(state, r, now, silent=True)
        to_send = []

    if budget_hit:
        log.warning("Time budget reached -- progress saved, the rest is read next run.")
    run_clean = complete and not throttled_msg and not budget_hit
    if run_clean:
        health["fail_streak"] = 0
        state["cursor"]["last_ok"] = now.isoformat(timespec="seconds")
        state["cursor"]["active_ok"] = now.isoformat(timespec="seconds")
        state["initialized"] = True
    else:
        health["fail_streak"] = health.get("fail_streak", 0) + 1
        if health["fail_streak"] >= 3:
            _maybe_alert(cfg, state, f"{health['fail_streak']} incomplete runs in a row "
                                     f"({throttled_msg[0] if throttled_msg else 'listing stopped early'})")
    if g.token_rejected:
        _maybe_alert(cfg, state, "GitLab rejected GITLAB_TOKEN (expired/revoked). Running anonymously with lower limits. "
                                 "Create a new token (scope read_api).", key="token")
    state["near_miss"] = (state["near_miss"] + near)[-60:]

    if dry_run:
        for r in to_send:
            print(build_message(r, True))
            print(f"    score={r.score} stage={r.stage} {' '.join(r.reasons[:14])}")
        print(f"\n[dry-run] {len(repos)} listed, {len(todo)} read, {len(to_send)} matches, "
              f"{len(unreadable)} unreadable, {g.requests_made} API requests")
        return len(to_send)

    # WRITE-AHEAD: save as posted BEFORE sending. Worst case after a crash is a missed message, never a repeat.
    for r in to_send:
        _remember(state, r, now)
    if to_send:
        health["last_match"] = now.isoformat(timespec="seconds")
    _maybe_digest(cfg, state, now)
    _maybe_silence_alert(cfg, state, now)
    save_state(state)
    for r in to_send:
        try:
            post_webhook(cfg["webhook"], build_message(r, cfg["show_details"]), cfg["username"])
        except Exception as e:  # noqa: BLE001 - already saved as posted; never double-post
            log.error("Discord send failed for %s: %s", r.url, e)
            alert(cfg, f"Could not send {r.url} to Discord: {e}")
    log.info("Done: %d listed, %d read, %d unreadable (queued), %d posted, %d API requests",
             len(repos), len(todo), len(unreadable), len(to_send), g.requests_made)
    return len(to_send)


def _maybe_alert(cfg: dict, state: dict, text: str, key: str = "run", cooldown_h: float = 6) -> None:
    h = state["health"]
    last = parse_ts(h.get(f"last_alert_{key}"))
    now = datetime.now(timezone.utc)
    if last and now - last < timedelta(hours=cooldown_h):
        return
    h[f"last_alert_{key}"] = now.isoformat(timespec="seconds")
    alert(cfg, text)


def _maybe_silence_alert(cfg: dict, state: dict, now: datetime) -> None:
    """Tell the owner if nothing has matched for a long time (maybe the bot is blind, maybe it is just quiet)."""
    h = state["health"]
    last = parse_ts(h.get("last_match")) or parse_ts(state["cursor"].get("last_ok"))
    hours = cfg["silence_alert_hours"]
    if hours and last and now - last > timedelta(hours=hours):
        _maybe_alert(cfg, state, f"No match for {hours:g}h. The bot is running normally; "
                                 f"lower MIN_SCORE or run --near-misses to see what it rejected.", key="silence", cooldown_h=hours)


def _maybe_digest(cfg: dict, state: dict, now: datetime) -> None:
    """Once a day: the best near-misses go to the alert webhook, so you can tune what counts."""
    hook = cfg.get("alert_webhook")
    h = state["health"]
    last = parse_ts(h.get("last_digest"))
    if not hook or (last and now - last < timedelta(hours=24)) or not state["near_miss"]:
        return
    best = sorted({n["url"]: n for n in state["near_miss"]}.values(), key=lambda n: -n["score"])[:8]
    lines = [f"Near misses in the last day (need {cfg['min_score']}):"] + \
            [f"- {n['score']}: {n['url']}  ({n['why'][:70]})" for n in best]
    h["last_digest"] = now.isoformat(timespec="seconds")
    state["near_miss"] = []
    try:
        post_webhook(hook, "\n".join(lines)[:1900], cfg.get("username"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not send digest: %s", e)



# --------------------------------------------------------------------------- #
# Discovery: search a longer window and judge every hit with the deep reader
# --------------------------------------------------------------------------- #
def discover(cfg: dict, g: GitLab, days: float, show_near: bool = True) -> list[Repo]:
    import patterns as P
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"order_by": "created_at", "sort": "desc", "per_page": 100, "created_after": since,
            "archived": "false", "visibility": "public", "simple": "true"}
    queries = [("search", w) for w in P.SEARCH_WORDS] + [("topic", t) for t in P.SEARCH_TOPICS]
    found: dict[int, Repo] = {}
    for key, value in queries:
        try:
            n = 0
            for page in g.pages("/projects", {**base, key: value}, max_pages=3):
                for d in page:
                    created = parse_ts(d.get("created_at"))
                    if created and created < cutoff:       # GitLab search ignores created_after -> filter here
                        continue
                    found.setdefault(int(d["id"]), Repo.from_api(d))
                    n += 1
            log.info("  %-10s %-18s %4d hits  (unique so far %d)", key, value, n, len(found))
        except Throttled as e:
            log.warning("%s -- using what was found so far", e)
            break
        except Unreadable as e:
            log.warning("search %s=%s failed: %s", key, value, e)
    repos = [r for r in found.values() if r.default_branch and r.owner.lower() not in cfg["block_owners"]]
    log.info("Discovery: %d candidate projects, reading their files...", len(repos))

    def work(repo: Repo):
        try:
            evaluate(g, repo, cfg)
            if repo.kind and not confirm_not_fork(g, repo):
                repo.kind, repo.skip_reason = None, "fork / mirror"
        except (Throttled, Unreadable, Exception) as e:  # noqa: BLE001
            repo.skip_reason = f"unreadable: {e}"
        return repo

    out: list[Repo] = []
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        for i, repo in enumerate(pool.map(work, repos), 1):
            out.append(repo)
            if i % 200 == 0:
                log.info("  ... %d/%d read", i, len(repos))
    matches = sorted((r for r in out if r.kind), key=lambda r: -r.score)
    print(f"\n=== {len(matches)} MATCHES (score >= {cfg['min_score']}, stages: {', '.join(cfg['stages'])}) ===")
    for r in matches:
        print(f"{r.score:3d} {r.kind.upper():4s} {r.stage:11s} {r.algo or '-':10s} {r.created_at[:10]} "
              f"{r.url}\n      {r.description[:100]!r}\n      evidence: {summary_line(r) or '-'}")
    if show_near:
        near = sorted((r for r in out if not r.kind and r.score >= cfg["min_score"] - 4 and not r.flags.get("hard_neg")
                       and (r.flags.get("real_chain") or r.flags.get("pow_ev"))), key=lambda r: -r.score)[:15]
        print(f"\n=== near misses ({len(near)}) ===")
        for r in near:
            print(f"{r.score:3d} {r.stage:11s} {r.skip_reason or ''} {r.url}  {' '.join(r.reasons[:8])}")
    print(f"\n[{len(repos)} read, {g.requests_made} API requests]")
    return matches

# --------------------------------------------------------------------------- #
# Explain / CLI
# --------------------------------------------------------------------------- #
def explain(ident: str, cfg: dict, g: GitLab) -> None:
    d = g.project(ident)
    if d is None:
        raise SystemExit(f"Project not found: {ident}")
    repo = Repo.from_api(d)
    repo.fork = bool(d.get("forked_from_project"))
    now = datetime.now(timezone.utc)
    try:
        evaluate(g, repo, cfg, now, force_recency=True)
    except (Throttled, Unreadable) as e:
        raise SystemExit(f"Could not read project: {e}")
    print(repo.url)
    print(f"  created {repo.created_at[:10]}  last activity {repo.pushed_at[:16]}  stars {repo.stars}  fork {repo.fork}  "
          f"files read: {len(repo.files_read)}")
    print(f"  files: {', '.join(repo.paths[:25])}{' ...' if len(repo.paths) > 25 else ''}")
    print(f"  read:  {', '.join(repo.files_read) or '-'}")
    print(f"  project score {repo.score} (need {cfg['min_score']})  algo={repo.algo}  gates: {repo.flags}")
    print("  " + " ".join(repo.reasons))
    rec = repo.recency
    if rec is not None:
        print("  --- stage / recency ---")
        print(f"  detected stage: {repo.stage if repo.kind or rec.accept else rec.stage}")
        for line in recency.explain_lines(rec, cfg):
            print(line)
        if rec.errors:
            print(f"  notes: {rec.errors}")
    else:
        print("  recency: not checked (" + (repo.skip_reason or "empty") + ")")
    if repo.kind:
        try:
            fork_ok = confirm_not_fork(g, repo)
        except (Throttled, Unreadable):
            fork_ok = True
        if not fork_ok:
            repo.kind, repo.skip_reason = None, "fork / mirror without its own recent launch event"
    state = None
    try:
        state = load_state()
    except StateError:
        pass
    block = blocked_reason(state, repo, cfg, now, stage=repo.stage) if (state and repo.kind) else None
    if repo.kind and block:
        verdict = f"WOULD NOT POST ({block})"
    elif repo.kind:
        verdict = f"WOULD POST as {repo.kind.upper()} ({repo.stage})"
    else:
        why = repo.skip_reason or ("project score / evidence too low" if not (rec and rec.accept) else "not a PoW/node project")
        verdict = f"NOT POSTED -- {why}"
    print(f"  ==> {verdict}")
    if repo.llm:
        print(f"  AI judge: {repo.llm}")


def load_dotenv() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def make_config() -> dict:
    stages = [s.strip().lower() for s in os.getenv("STAGES", "pre-testnet,testnet,mainnet").split(",") if s.strip()]
    kws = [k.strip().lower() for k in os.getenv("ACTIVE_KEYWORDS", ",".join(P.ACTIVE_KEYWORDS)).split(",") if k.strip()]
    return {
        "webhook": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        "alert_webhook": os.getenv("ALERT_WEBHOOK_URL", "").strip(),
        "username": os.getenv("BOT_NAME", "").strip() or None,
        "interval": float(os.getenv("CHECK_INTERVAL_MINUTES", "30")),
        "max_age_hours": float(os.getenv("MAX_AGE_HOURS", "48")),
        "max_projects": int(os.getenv("MAX_PROJECTS", "0")),
        "recent_days": float(os.getenv("RECENT_DAYS", "7")),
        "require_event": _bool("REQUIRE_EVENT", "true"),
        "active_stream": _bool("ACTIVE_STREAM", "true"),
        "active_keywords": kws,
        "active_max_pages": max(1, int(os.getenv("ACTIVE_MAX_PAGES", "5"))),
        "watch_hours": float(os.getenv("WATCH_HOURS", "12")),
        "recent_commits": 20,
        "diff_commits": 8,
        "first_run_hours": float(os.getenv("FIRST_RUN_HOURS", "3")),
        "budget_s": float(os.getenv("RUN_BUDGET_MINUTES", "0")) * 60,
        "min_score": int(os.getenv("MIN_SCORE", "8")),
        "include_node": _bool("INCLUDE_NODE", "true"),
        "stages": stages,
        "one_per_owner_days": float(os.getenv("ONE_PER_OWNER_DAYS", "30")),
        "min_stars": int(os.getenv("MIN_STARS", "0")),
        "show_details": _bool("SHOW_DETAILS", "true"),
        "post_on_first_run": _bool("POST_ON_FIRST_RUN", "false"),
        "workers": max(1, min(16, int(os.getenv("WORKERS", "6")))),
        "block_owners": {o.strip().lower() for o in os.getenv("BLOCK_OWNERS", "").split(",") if o.strip()},
        "silence_alert_hours": float(os.getenv("SILENCE_ALERT_HOURS", "0")),
        "use_llm": llm.available() and _bool("USE_LLM", "true"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="GitLab new PoW/node projects -> Discord")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--dry-run", action="store_true", help="print matches; send and save nothing")
    p.add_argument("--explain", metavar="GROUP/PROJECT", help="show the evidence for one project and exit")
    p.add_argument("--discover", metavar="DAYS", type=float, help="search the last DAYS days (e.g. 60), print what matches, change nothing")
    p.add_argument("--near-misses", action="store_true", help="show recent near-miss projects from the saved state")
    p.add_argument("--merge-state", metavar="FILE", help="merge another seen.json into ours and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="also log rejected projects")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv()
    if args.merge_state:
        merge_state_files(Path(args.merge_state))
        return
    if args.near_misses:
        for n in sorted({n["url"]: n for n in load_state()["near_miss"]}.values(), key=lambda n: -n["score"]):
            print(f"{n['score']:3d} {n['stage']:12s} {n['url']}  {n['why'][:90]}")
        return

    g = GitLab((os.getenv("GITLAB_URL") or "https://gitlab.com").strip(), os.getenv("GITLAB_TOKEN"))
    cfg = make_config()
    if args.explain:
        explain(args.explain, cfg, g)
        return
    if args.discover:
        discover(cfg, g, args.discover)
        return
    if not cfg["webhook"] and not args.dry_run:
        sys.exit("ERROR: set DISCORD_WEBHOOK_URL (env var or .env file), or use --dry-run")

    try:
        with SingleInstance():
            if args.once:
                check_once(cfg, g, args.dry_run)
                return
            log.info("Running every %.0f minutes. Ctrl+C to stop.", cfg["interval"])
            while True:
                try:
                    check_once(cfg, g, args.dry_run)
                except StateError:
                    raise
                except Exception as e:  # noqa: BLE001 - the loop never dies
                    log.error("Check failed: %s: %s", type(e).__name__, e)
                time.sleep(cfg["interval"] * 60)
    except (AlreadyRunning, StateError) as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
