"""
GitLab API client: thread-safe, retries with backoff + jitter, honours rate limits, and
falls back to anonymous access if the token is rejected (so a revoked/expired token
does not stop the bot).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

log = logging.getLogger("gl-bot")
MAX_WAIT = 90   # never sleep longer than this for a rate limit; stop and retry next run instead


class Throttled(RuntimeError):
    """GitLab is rate limiting us for too long. Stop for now; unfinished work is retried next run."""


class Unreadable(RuntimeError):
    """One project could not be read right now (network / server error). It is retried later."""


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class GitLab:
    def __init__(self, base: str = "https://gitlab.com", token: str | None = None, timeout: int = 30):
        self.base = base.rstrip("/")
        self.api = self.base + "/api/v4"
        self.token = (token or "").strip() or None
        self.token_rejected = False
        self.timeout = timeout
        self.requests_made = 0
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pause_until = 0.0

    # ------------------------------------------------------------------ #
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Accept": "application/json", "User-Agent": "pow-node-project-discord-notifier/2.0"})
            self._local.s = s
        return s

    def get(self, path: str, params: dict | None = None, limit: int | None = None, retries: int = 4):
        """GET with retries. Returns a Response (any status < 500 except 429) or None if the
        server stays unreachable. Raises Throttled when rate-limited for too long.
        If `limit` is set the body is read only up to that many bytes into `r.body`."""
        url = path if path.startswith("http") else self.api + path
        for attempt in range(retries):
            wait = self._pause_until - time.time()
            if wait > 0:
                time.sleep(min(wait, MAX_WAIT))
            headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
            try:
                r = self._session().get(url, params=params, headers=headers, timeout=self.timeout,
                                        stream=limit is not None)
            except requests.RequestException as e:
                log.debug("request error %s: %s", url, e)
                time.sleep(min(2 ** attempt, 20) * (0.5 + random.random()))
                continue
            self.requests_made += 1
            if r.status_code == 401 and self.token:
                with self._lock:
                    if self.token:
                        log.warning("GitLab rejected the token (401) -- continuing anonymously. "
                                    "Check GITLAB_TOKEN (expired or revoked?).")
                        self.token = None
                        self.token_rejected = True
                r.close()
                continue
            if r.status_code == 429:
                wait = r.headers.get("Retry-After")
                if wait is None:
                    reset = r.headers.get("RateLimit-Reset")
                    wait = max(5, int(reset) - time.time() + 2) if reset and reset.isdigit() else 30
                wait = int(float(wait))
                r.close()
                if wait > MAX_WAIT:
                    raise Throttled(f"GitLab rate limit reached, resets in {wait}s. Set GITLAB_TOKEN to raise the limit.")
                log.info("Rate limited, all workers wait %ss", wait)
                self._pause_until = max(self._pause_until, time.time() + wait + 1)
                continue
            if r.status_code >= 500 or r.status_code == 408:
                r.close()
                time.sleep(min(2 ** attempt, 20) * (0.5 + random.random()))
                continue
            if limit is not None:
                body, total = [], 0
                try:
                    for chunk in r.iter_content(16384):
                        body.append(chunk)
                        total += len(chunk)
                        if total >= limit:
                            break
                except requests.RequestException as e:
                    log.debug("stream error %s: %s", url, e)
                    r.close()
                    continue
                r.close()
                r.body = b"".join(body)[:limit]
            else:
                r.body = None
            return r
        return None

    # ------------------------------------------------------------------ #
    def pages(self, path: str, params: dict, max_pages: int = 10_000):
        """Yield decoded JSON pages, following keyset (Link header) or offset pagination.
        Raises Unreadable if a page cannot be fetched."""
        r = self.get(path, params)
        n = 0
        while True:
            if r is None or r.status_code != 200:
                raise Unreadable(f"{path}: HTTP {r.status_code if r is not None else 'no response'}")
            try:
                data = r.json()
            except ValueError as e:
                raise Unreadable(f"{path}: bad JSON ({e})") from None
            yield data
            n += 1
            if n >= max_pages:
                return
            nxt = r.links.get("next", {}).get("url")
            if nxt:
                r = self.get(nxt)
                continue
            xp = r.headers.get("x-next-page")
            if xp and "keyset" not in str(params.get("pagination", "")):
                r = self.get(path, {**params, "page": int(xp)})
                continue
            return

    # ---- project listing ------------------------------------------------ #
    def top_id(self) -> int:
        r = self.get("/projects", {"order_by": "id", "sort": "desc", "per_page": 1, "visibility": "public", "simple": "true"})
        if r is None or r.status_code != 200 or not r.json():
            raise Unreadable("cannot read newest project id")
        return int(r.json()[0]["id"])

    def _created_after_id(self, x: int) -> datetime | None:
        r = self.get("/projects", {"order_by": "id", "sort": "asc", "pagination": "keyset", "per_page": 1,
                                    "visibility": "public", "simple": "true", "id_after": x})
        if r is None or r.status_code != 200:
            raise Unreadable("cannot probe project ids")
        items = r.json()
        return parse_ts(items[0]["created_at"]) if items else None

    def find_window_id(self, since: datetime) -> int:
        """A project id (a little below) the first project created at/after `since`.
        Ids grow with time, so a short bisection finds it; the project list can then be
        read with id_after instead of paging through millions of old projects."""
        top = self.top_id()
        step, hi = 20_000, top
        lo = max(0, top - step)
        while lo > 0:
            t = self._created_after_id(lo)
            if t is not None and t < since:
                break
            hi, step = lo, step * 2
            lo = max(0, top - step)
        for _ in range(14):
            if hi - lo <= 400:
                break
            mid = (lo + hi) // 2
            t = self._created_after_id(mid)
            if t is None or t >= since:
                hi = mid
            else:
                lo = mid
        return max(0, lo - 2000)    # ids are only roughly in time order -> safety margin

    def list_new_projects(self, id_after: int, activity_after: datetime):
        """Pages of public, non-archived projects with id > id_after and activity since activity_after."""
        params = {"order_by": "id", "sort": "asc", "pagination": "keyset", "per_page": 100,
                  "visibility": "public", "archived": "false", "simple": "true",
                  "id_after": id_after, "last_activity_after": activity_after.strftime("%Y-%m-%dT%H:%M:%SZ")}
        yield from self.pages("/projects", params)

    def project(self, ident) -> dict | None:
        r = self.get(f"/projects/{quote(str(ident), safe='')}")
        if r is None:
            raise Unreadable(f"project {ident}: no response")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise Unreadable(f"project {ident}: HTTP {r.status_code}")
        return r.json()

    # ---- recently ACTIVE projects (old repositories that are updating now) ---- #
    def list_active(self, keyword: str, activity_after: datetime, max_pages: int = 5):
        """Pages of public projects matching `keyword` (name / path / description), newest activity first,
        with activity since `activity_after`. Offset pagination, capped at `max_pages` x 100 projects."""
        params = {"search": keyword, "order_by": "last_activity_at", "sort": "desc", "per_page": 100,
                  "visibility": "public", "archived": "false", "simple": "true",
                  "last_activity_after": activity_after.strftime("%Y-%m-%dT%H:%M:%SZ")}
        yield from self.pages("/projects", params, max_pages=max_pages)

    # ---- history: commits / tags / releases ---------------------------------- #
    def _list(self, path: str, params: dict, what: str) -> list:
        r = self.get(path, params)
        if r is None:
            raise Unreadable(f"{what}: no response")
        if r.status_code in (404, 403):      # feature disabled / no access: treat as empty
            return []
        if r.status_code != 200:
            raise Unreadable(f"{what}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise Unreadable(f"{what}: bad JSON ({e})") from None
        return data if isinstance(data, list) else []

    def commits(self, pid: int, ref: str | None, since: datetime | None = None, until: datetime | None = None,
                path: str | None = None, per_page: int = 20) -> list[dict]:
        """Commits of a branch, newest first. `since`/`until` filter by commit date, `path` by touched file."""
        params: dict = {"per_page": per_page}
        if ref:
            params["ref_name"] = ref
        if since:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        if until:
            params["until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        if path:
            params["path"] = path
        return self._list(f"/projects/{pid}/repository/commits", params, f"commits {pid}")

    def tags(self, pid: int, per_page: int = 100) -> list[dict]:
        return self._list(f"/projects/{pid}/repository/tags",
                          {"order_by": "updated", "sort": "desc", "per_page": per_page}, f"tags {pid}")

    def releases(self, pid: int, per_page: int = 100) -> list[dict]:
        return self._list(f"/projects/{pid}/releases",
                          {"order_by": "released_at", "sort": "desc", "per_page": per_page}, f"releases {pid}")

    def commit_diff(self, pid: int, sha: str) -> list[dict]:
        """Files changed by one commit: [{old_path, new_path, new_file, diff, ...}]. Large diffs come back empty."""
        return self._list(f"/projects/{pid}/repository/commits/{sha}/diff", {"per_page": 100}, f"diff {pid}@{sha[:8]}")

    def first_commit(self, pid: int, ref: str | None, path: str) -> dict | None:
        """The OLDEST commit that touched `path` (= when the file was introduced). Two requests: the commit list
        reports its page count with per_page=1, the last page is the oldest commit. If GitLab does not report a
        total (huge histories) the newest commit older than nothing is returned, i.e. the newest one (a lower bound)."""
        params: dict = {"per_page": 1, "path": path}
        if ref:
            params["ref_name"] = ref
        url = f"/projects/{pid}/repository/commits"
        r = self.get(url, params)
        if r is None:
            raise Unreadable(f"first_commit {pid}: no response")
        if r.status_code in (404, 403):
            return None
        if r.status_code != 200:
            raise Unreadable(f"first_commit {pid}: HTTP {r.status_code}")
        data = r.json()
        total = r.headers.get("x-total-pages", "")
        if data and total.isdigit() and int(total) > 1:
            r2 = self.get(url, {**params, "page": int(total)})
            if r2 is not None and r2.status_code == 200 and r2.json():
                data = r2.json()
        return data[0] if data else None

    # ---- repository contents ------------------------------------------- #
    def tree(self, pid: int, ref: str, path: str = "", recursive: bool = False, max_pages: int = 3):
        """List a directory. Returns (entries, truncated). Entries are {name, path, type}.
        Directory paths get a trailing "/". `truncated` is True if more pages existed."""
        params = {"ref": ref, "per_page": 100, "pagination": "keyset"}
        if path:
            params["path"] = path
        if recursive:
            params["recursive"] = "true"
        out: list[dict] = []
        r = self.get(f"/projects/{pid}/repository/tree", params)
        for i in range(max_pages):
            if r is None:
                raise Unreadable(f"tree {pid}/{path}: no response")
            if r.status_code in (404, 403):
                return out, False
            if r.status_code != 200:
                raise Unreadable(f"tree {pid}/{path}: HTTP {r.status_code}")
            for e in r.json():
                if e.get("type") == "tree":
                    e["path"] = e["path"].rstrip("/") + "/"
                out.append(e)
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                return out, False
            if i == max_pages - 1:
                return out, True
            r = self.get(nxt)
        return out, True

    def raw_file(self, pid: int, path: str, ref: str, limit: int = 80_000) -> str | None:
        """File text (first `limit` bytes) or None if the file does not exist."""
        r = self.get(f"/projects/{pid}/repository/files/{quote(path, safe='')}/raw", {"ref": ref}, limit=limit)
        if r is None:
            raise Unreadable(f"file {pid}/{path}: no response")
        if r.status_code in (404, 403):
            return None
        if r.status_code != 200:
            raise Unreadable(f"file {pid}/{path}: HTTP {r.status_code}")
        return (r.body or b"").decode("utf-8", errors="replace")
