"""
Offline end-to-end test of the post / no-repeat / retry logic. Run:  python tests/test_flow.py
Uses a local fake Discord webhook and a fake GitLab, so nothing real is contacted.
"""
import json, os, sys, tempfile, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot                      # noqa: E402
from classify import Repo       # noqa: E402
from gl import Unreadable       # noqa: E402

POSTS = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        POSTS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{srv.server_port}/hook"

tmp = Path(tempfile.mkdtemp())
bot.STATE_FILE, bot.BACKUP_FILE, bot.LOCK_FILE = tmp/"seen.json", tmp/"seen.json.bak", tmp/"bot.lock"
bot.time.sleep = lambda s: None

README_POW = ("# Zorbcoin\nFair launch RandomX CPU-mined coin, no premine. Block time 60 seconds, block reward 50 ZRB, "
              "halving. Testnet is live, use the faucet. Genesis block, seed nodes, stratum pool.")
def mk(i, owner, name, readme, files=("README.md","src/")):
    return Repo(id=i, full_name=f"{owner}/{name}", url=f"https://gitlab.com/{owner}/{name}", owner=owner,
                created_at=datetime.now(timezone.utc).isoformat(), pushed_at="2026-11-19T10:00:00Z",
                default_branch="main"), readme

class FakeG:
    token_rejected = False; requests_made = 0
    # recency endpoints: fresh commit, no tags / releases, no chain-file history
    def commits(self, pid, ref, since=None, until=None, path=None, per_page=20):
        if path: return []
        return [{"id": "a"*40, "short_id": "aaaaaaaa", "title": "init", "message": "init",
                 "committed_date": datetime.now(timezone.utc).isoformat()}]
    def tags(self, pid, per_page=100): return []
    def releases(self, pid, per_page=100): return []
    def commit_diff(self, pid, sha): return []
    def first_commit(self, pid, ref, path): return None
    def project(self, ident): return {"id": int(ident), "default_branch": "main", "path_with_namespace": "x/y",
                                      "web_url": "u", "namespace": {"full_path": "x"}}
fails = {"n": 0}
DATA = {}
def fake_read(g, repo):
    if repo.id == 3 and fails["n"] < 1:
        fails["n"] += 1; raise Unreadable("boom")
    repo.text = DATA[repo.id]; repo.paths = ["README.md", "src/", "src/chainparams.cpp", "src/pow.cpp"]
bot.read_project = fake_read
bot.confirm_not_fork = lambda g, r: True

repos = []
for i, (o, n, t) in enumerate([("a","zorbcoin",README_POW), ("b","captcha-guard","Proof of work captcha anti-bot sha256 for websites"),
                               ("c","flaky",README_POW), ("a","zorb-wallet",README_POW)], 1):
    r, txt = mk(i, o, n, t); repos.append(r); DATA[i] = txt
bot.list_candidates = lambda g, state, cfg, now: (list(repos), True)

os.environ.update(DISCORD_WEBHOOK_URL=HOOK, MIN_SCORE="8", ACTIVE_STREAM="false")
cfg = bot.make_config()
st = bot.load_state(); st["initialized"] = True; bot.save_state(st)

n1 = bot.check_once(cfg, FakeG())
st = bot.load_state()
print("run1 posted:", [p["content"].split("\n")[0] for p in POSTS])
assert n1 == 1 and "zorbcoin" in POSTS[0]["content"], "only the real coin should post (captcha rejected, 2nd 'a' project blocked by one-per-owner)"
assert "3" in st["queue"], "unreadable project must go to the retry queue, not be dropped"
assert "2" in st["seen"] and "3" not in st["seen"]

n2 = bot.check_once(cfg, FakeG())       # flaky project now readable
print("run2 posted:", [p["content"].split("\n")[0] for p in POSTS[1:]])
assert n2 == 1 and "flaky" in POSTS[1]["content"], "queued project must be retried and posted"
assert not bot.load_state()["queue"]

n3 = bot.check_once(cfg, FakeG())
assert n3 == 0 and len(POSTS) == 2, "nothing may be posted twice"

# damaged state file: falls back to backup, never re-posts
bot.STATE_FILE.write_text("{broken")
n4 = bot.check_once(cfg, FakeG()); assert n4 == 0 and len(POSTS) == 2
# both damaged: refuses to start
bot.STATE_FILE.write_text("{broken"); bot.BACKUP_FILE.write_text("{broken")
try: bot.load_state(); raise SystemExit("should have refused")
except bot.StateError: pass
print("ALL FLOW TESTS PASSED  (messages:", [p["content"].replace("\n", " | ")[:90] for p in POSTS], ")")
