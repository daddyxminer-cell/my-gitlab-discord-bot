"""Live test: real GitLab reads + real classify + real state/post logic, fake Discord on localhost."""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot
from classify import Repo
from gl import GitLab

POSTS = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        POSTS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))); self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
bot.load_dotenv()
os.environ["DISCORD_WEBHOOK_URL"] = f"http://127.0.0.1:{srv.server_port}/h"
g = GitLab("https://gitlab.com", os.getenv("GITLAB_TOKEN"))
IDS = ["tesserax-labs/tesserax", "nival-group1/NIVAL-project", "elyan-labs/Rustchain", "16ane/16ane-net",
       "payiz-12/bitnetscan", "ironfist-2000/truenorth-ironminer", "OneThirtySeven/nexa/nexa-sol", "internet-money-network/imoney"]
repos = [Repo.from_api(g.project(i)) for i in IDS]
bot.list_candidates = lambda *a: (list(repos), True)
cfg = bot.make_config()
st = bot.load_state(); st["initialized"] = True; bot.save_state(st)
n = bot.check_once(cfg, g)
print("RUN1 posted", n, [p["content"].replace("\n", " | ") for p in POSTS])
n = bot.check_once(cfg, g)
print("RUN2 posted", n, "(must be 0)  total messages:", len(POSTS))
