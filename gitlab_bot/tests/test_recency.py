"""
Offline tests for the 7-day recency logic (recency.py) and the stage-keyed state (bot.py).
Run:  python tests/test_recency.py        (no network, no Discord)
"""
import json, os, sys, tempfile, threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot                      # noqa: E402
import recency as R             # noqa: E402
from classify import Repo       # noqa: E402

NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


H64 = "ab" * 32
GENESIS_DIFF = ("@@ -10,6 +10,9 @@ class CMainParams : public CChainParams {\n"
                f"+        genesis = CreateGenesisBlock(1700000000, 123, 0x1e0ffff0, 1, 50 * COIN);\n"
                f"+        assert(consensus.hashGenesisBlock == uint256S(\"0x{H64}\"));\n")
TESTNET_DIFF = ("@@ -40,6 +40,8 @@ class CTestNetParams : public CChainParams {\n"
                f"+        genesis = CreateGenesisBlock(1700000500, 7, 0x1e0ffff0, 1, 50 * COIN);\n"
                f"+        assert(consensus.hashGenesisBlock == uint256S(\"0x{'cd' * 32}\"));\n")


class FakeGL:
    """Stands in for gl.GitLab. Only the recency endpoints."""
    def __init__(self, commits=(), tags=(), releases=(), diffs=None, first=None, path_commits=None):
        self._commits, self._tags, self._releases = list(commits), list(tags), list(releases)
        self._diffs, self._first, self._path_commits = diffs or {}, first, path_commits or {}
        self.calls = []

    def commits(self, pid, ref, since=None, until=None, path=None, per_page=20):
        self.calls.append(("commits", path))
        if path:
            return self._path_commits.get(path, [])
        return [c for c in self._commits if since is None or R.parse_ts(c["committed_date"]) >= since][:per_page]

    def tags(self, pid, per_page=100): return self._tags
    def releases(self, pid, per_page=100): return self._releases
    def commit_diff(self, pid, sha): return self._diffs.get(sha, [])
    def first_commit(self, pid, ref, path):
        self.calls.append(("first_commit", path)); return self._first


def commit(sha, days, title="work"):
    return {"id": sha * 40, "short_id": sha * 8, "title": title, "message": title, "committed_date": ago(days)}


def repo(stage="testnet", created_days=2000, paths=("README.md", "src/chainparams.cpp")):
    r = Repo(id=1, full_name="g/coin", url="https://gitlab.com/g/coin", owner="g", created_at=ago(created_days),
             pushed_at=ago(0.1), default_branch="main")
    r.stage, r.paths = stage, list(paths)
    return r


CFG = {"recent_days": 7, "require_event": True, "recent_commits": 20, "diff_commits": 8}


def run(fake, **kw):
    r = repo(**kw)
    return R.assess(fake, r, CFG, NOW, want_latest=True)


# --------------------------------------------------------------------------- #
# 1. activity gate: only latest commit / release / tag matter (no age rule)
# --------------------------------------------------------------------------- #
rec = run(FakeGL(commits=[commit("a", 30)]), stage="testnet")
assert not rec.accept and "no commit and no release/tag" in rec.reason, rec.reason

# very OLD repository (created 6 years ago) with fresh activity + fresh testnet release is accepted
rec = run(FakeGL(commits=[commit("a", 1)], tags=[{"name": "v0.9.0-testnet", "created_at": ago(2), "commit": {}}]),
          stage="testnet", created_days=2200)
assert rec.accept and rec.stage == "testnet", rec.reason

# a release/tag alone (no commit in 7 days) keeps the project active
rec = run(FakeGL(commits=[commit("a", 40)],
                 releases=[{"tag_name": "testnet-2", "name": "Public testnet 2", "description": "Testnet is live, faucet open",
                            "released_at": ago(3)}]), stage="testnet")
assert rec.active and rec.accept, rec.reason

# old repo, activity only (no stage event) -> not a stage change
rec = run(FakeGL(commits=[commit("a", 1, "fix typo")]), stage="testnet")
assert not rec.accept and "no testnet/genesis/launch event" in rec.reason, rec.reason
# ...unless REQUIRE_EVENT is off
r = repo(stage="testnet")
rec = R.assess(FakeGL(commits=[commit("a", 1, "fix typo")]), r, {**CFG, "require_event": False}, NOW)
assert rec.accept
# brand new repo with activity: accepted without an extra event
rec = run(FakeGL(commits=[commit("a", 1)]), stage="pre-testnet", created_days=2)
assert rec.accept and rec.stage == "pre-testnet", rec.reason

# commit MESSAGE alone is a weak hint -> not enough for an old repo
rec = run(FakeGL(commits=[commit("a", 1, "Add testnet config")]), stage="testnet")
assert not rec.accept, rec.reason

# --------------------------------------------------------------------------- #
# 2. mainnet rule: launch <= 7 days -> accept, older -> skip
# --------------------------------------------------------------------------- #
# a) fresh genesis in a recent diff of chainparams (new chain / reset)
fk = FakeGL(commits=[commit("b", 1, "genesis")], path_commits={"src/chainparams.cpp": [commit("b", 1)]},
            diffs={"b" * 40: [{"new_path": "src/chainparams.cpp", "old_path": "src/chainparams.cpp", "diff": GENESIS_DIFF}]})
rec = run(fk, stage="mainnet")
assert rec.accept and rec.stage == "mainnet" and rec.mainnet_age() <= 7, (rec.reason, rec.events)

# b) release v1.0.0 two days ago, README claims mainnet -> accept
rec = run(FakeGL(commits=[commit("a", 1)], tags=[{"name": "v1.0.0", "created_at": ago(2), "commit": {}}]), stage="mainnet")
assert rec.accept and "v1.0.0" in rec.mainnet_src, (rec.reason, rec.mainnet_src)

# c) release named "mainnet" 20 days ago, fresh commits -> too old, skip
rec = run(FakeGL(commits=[commit("a", 1)], tags=[{"name": "mainnet", "created_at": ago(20), "commit": {}}]), stage="mainnet")
assert not rec.accept and rec.established and rec.mainnet_age() > 7, rec.reason

# d) v1.0.0 launched long ago, a fresh v1.2.0 whose notes mention mainnet must not re-date the launch
rec = run(FakeGL(commits=[commit("a", 1)],
                 releases=[{"tag_name": "v1.0.0", "description": "Mainnet launched", "released_at": ago(400)},
                           {"tag_name": "v1.2.0", "description": "Mainnet launched! (re-announce)", "released_at": ago(1)}]),
          stage="mainnet")
assert not rec.accept and rec.mainnet_age() > 300, (rec.reason, rec.mainnet_age())

# e) no tags: dated by the commit that introduced chainparams (first_commit)
rec = run(FakeGL(commits=[commit("a", 1)], first={"committed_date": ago(3), "id": "c" * 40}), stage="mainnet")
assert rec.accept and "introduced" in rec.mainnet_src, (rec.reason, rec.mainnet_src)
rec = run(FakeGL(commits=[commit("a", 1)], first={"committed_date": ago(900), "id": "c" * 40}), stage="mainnet")
assert not rec.accept and rec.established, rec.reason

# f) launch cannot be dated at all -> falls back to repo creation date
rec = run(FakeGL(commits=[commit("a", 1)]), stage="mainnet", paths=("README.md",), created_days=1500)
assert not rec.accept and "repository created" in rec.mainnet_src, (rec.reason, rec.mainnet_src)
rec = run(FakeGL(commits=[commit("a", 1)]), stage="mainnet", paths=("README.md",), created_days=2)
assert rec.accept, rec.reason

# g) "mainnet will launch soon" is a plan, not a launch
rec = run(FakeGL(commits=[commit("a", 1, "Update docs: mainnet will launch soon")]), stage="testnet")
assert rec.stage == "testnet" and not rec.accept

# h) mature mainnet project that opens a NEW testnet this week (README only claims mainnet) -> testnet accepted
fk = FakeGL(commits=[commit("d", 1)], path_commits={"src/chainparams.cpp": [commit("d", 1)]},
            diffs={"d" * 40: [{"new_path": "src/chainparams.cpp", "old_path": "src/chainparams.cpp", "diff": TESTNET_DIFF}]})
rec = run(fk, stage="mainnet")
assert rec.accept and rec.stage == "testnet", (rec.reason, rec.stage)

# --------------------------------------------------------------------------- #
# 3. diff parsing
# --------------------------------------------------------------------------- #
moved = ("@@ -1,3 +1,3 @@ class CMainParams\n"
         f"-        assert(consensus.hashGenesisBlock == uint256S(\"0x{H64}\"));\n"
         f"+        assert(consensus.hashGenesisBlock == uint256S(\"0x{H64}\"));\n")
assert not any(i["hashes"] for i in R.scan_diff(moved).values()), "a moved hash is not a new genesis"
assert R.diff_events("src/chainparams.cpp", moved, False, NOW, "abc12345") == []
assert [e.stage for e in R.diff_events("README.md", "+Testnet is live, join the faucet", False, NOW, "abc12345")] == ["testnet"]
assert [e.stage for e in R.diff_events("README.md", "+Mainnet launched on 2026-11-18", False, NOW, "abc12345")] == ["mainnet"]
assert R.diff_events("README.md", "+Buy it on https://coingecko.com/x", False, NOW, "abc12345") == []
assert R.diff_events("src/main.cpp", "+testnet genesis", False, NOW, "abc12345") == [], "unrelated files are ignored"
regtest = "@@ -1,3 +1,3 @@ class CRegTestParams\n+  genesis = CreateGenesisBlock(1, 2, 3, 4, 5);\n"
assert R.diff_events("src/chainparams.cpp", regtest, False, NOW, "abc12345") == [], "regtest is not a launch"

# --------------------------------------------------------------------------- #
# 4. state: stage-keyed notifications, migration, re-post at the next stage
# --------------------------------------------------------------------------- #
POSTS = []
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        POSTS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), Hook); threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{srv.server_port}/h"

tmp = Path(tempfile.mkdtemp())
bot.STATE_FILE, bot.BACKUP_FILE, bot.LOCK_FILE = tmp / "seen.json", tmp / "seen.json.bak", tmp / "bot.lock"
bot.time.sleep = lambda s: None

# migration of a v2 state (notified keyed by plain project id)
old = bot._empty_state()
old.update(version=2, initialized=True, notified={"77": {"name": "Old/Coin", "url": "u", "stage": "testnet", "at": ago(5)},
                                                  "78": {"name": "Old/Main", "url": "u", "stage": "mainnet", "at": ago(5)}},
           notified_names=["old/coin", "old/main"])
old.pop("watch")
bot.STATE_FILE.write_text(json.dumps(old))
st = bot.load_state()
assert "77_testnet" in st["notified"] and "78_mainnet" in st["notified"] and "77" not in st["notified"], st["notified"]
assert "old/coin_testnet" in st["notified_names"] and "old/main_mainnet" in st["notified_names"]
assert st["notified"]["77_testnet"]["id"] == 77 and "watch" in st
bot.save_state(st)
assert bot.load_state()["notified"].keys() == st["notified"].keys(), "migration is idempotent"

# one project, three runs: testnet -> (no repeat) -> mainnet -> (no repeat)
os.environ.update(DISCORD_WEBHOOK_URL=HOOK, MIN_SCORE="8", ACTIVE_STREAM="false", WATCH_HOURS="0")
cfg = bot.make_config()
assert "mainnet" in cfg["stages"] and "max_history_days" not in cfg and cfg["recent_days"] == 7

README = ("# Zorbcoin\nFair launch RandomX CPU-mined coin, no premine. Block time 60 seconds, block reward 50 ZRB, halving. "
          "Genesis block, seed nodes, stratum pool. {stage}")
STAGE_TEXT = {"v": "Testnet is live, use the faucet."}
LIVE = {"pushed": ago(0.2), "tags": [{"name": "testnet-1", "created_at": ago(1), "commit": {}}],
        "files": ["README.md", "src/", "src/chainparams.cpp", "src/pow.cpp"]}


def fake_read(g, r):
    r.text = README.format(stage=STAGE_TEXT["v"]); r.paths = list(LIVE["files"])
bot.read_project = fake_read
bot.confirm_not_fork = lambda g, r: True


def mk():
    r = Repo(id=500, full_name="old/zorbcoin", url="https://gitlab.com/old/zorbcoin", owner="old",
             created_at=ago(2500), pushed_at=LIVE["pushed"], default_branch="main")
    r.origin = "active"
    return r


bot.list_candidates = lambda g, state, cfg, now: ([mk()], True)


class G(FakeGL):
    token_rejected = False; requests_made = 0
    def tags(self, pid, per_page=100): return LIVE["tags"]
    def commits(self, pid, ref, since=None, until=None, path=None, per_page=20):
        return [] if path else [commit("e", 0.2, "work")]


n = bot.check_once(cfg, G())
assert n == 1 and POSTS and "zorbcoin" in POSTS[-1]["content"] and "testnet" in POSTS[-1]["content"], POSTS
st = bot.load_state(); assert "500_testnet" in st["notified"], st["notified"]
assert "when:" in POSTS[-1]["content"], "message tells when the stage was reached"

LIVE["pushed"] = ago(0.1)                        # new activity, still testnet -> no repeat
assert bot.check_once(cfg, G()) == 0 and len(POSTS) == 1

STAGE_TEXT["v"] = "Mainnet launched! Mainnet seed nodes and explorer are online."
LIVE["tags"] = [{"name": "v1.0.0", "created_at": ago(1), "commit": {}}]
LIVE["pushed"] = ago(0.05)                       # project reached mainnet 1 day ago -> posted AGAIN
n = bot.check_once(cfg, G())
assert n == 1 and len(POSTS) == 2 and "mainnet" in POSTS[-1]["content"], [p["content"] for p in POSTS]
st = bot.load_state()
assert {"500_testnet", "500_mainnet"} <= set(st["notified"]) and st["seen"]["500"] == bot.FROZEN, st["notified"].keys()

LIVE["pushed"] = ago(0.01)
assert bot.check_once(cfg, G()) == 0 and len(POSTS) == 2, "nothing is posted twice at the last stage"

# --------------------------------------------------------------------------- #
# 5. active stream: 8 keyword queries, one request set each, de-duplicated by project id
# --------------------------------------------------------------------------- #
class StreamG:
    token_rejected = False; requests_made = 0
    def __init__(self): self.asked = []
    def list_active(self, kw, since, max_pages=5):
        self.asked.append((kw, since))
        yield [{"id": 900 + i, "path_with_namespace": f"x/p{i}", "web_url": "u", "namespace": {"full_path": "x"},
                "created_at": ago(900), "last_activity_at": ago(1), "default_branch": "main"} for i in range(3)] + \
              [{"id": 1000 + len(self.asked), "path_with_namespace": f"y/q{len(self.asked)}", "web_url": "u",
                "namespace": {"full_path": "y"}, "created_at": ago(900), "last_activity_at": ago(1), "default_branch": "main"}]

found = {}
g = StreamG()
cfg2 = bot.make_config()
ok = bot.list_active(g, bot._empty_state(), cfg2, NOW, found)
assert ok and [k for k, _ in g.asked] == ["testnet", "mainnet", "pre-testnet", "genesis", "pow", "randomx", "masternode", "validator"]
assert len(found) == 3 + 8, f"3 shared ids + 8 unique ones, got {len(found)}"
assert all(r.origin == "active" for r in found.values())
assert all(abs((NOW - s).total_seconds() - 7 * 86400) < 5 for _, s in g.asked), "window = last 7 days"

print("ALL RECENCY TESTS PASSED")
