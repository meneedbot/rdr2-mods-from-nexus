#!/usr/bin/env python3
"""
RDR2 Mod Hub - local server for the mod catalog site.

Why this exists
---------------
* Nexus has no CORS, so a static page cannot call it. This local server holds
  the API key on disk and answers same-origin requests from the page.
* The GitHub token lives here too, so the page never handles a credential and
  the published site (GitHub Pages) can stay completely secret-free.

Search uses the Nexus GraphQL API, which is the only endpoint that survived
their v1 API changes (v1 /mods/latest.json no longer exists; trending does).

Everything is free: Python standard library only, bound to 127.0.0.1.

Run:  python server.py
Open: http://127.0.0.1:8777/
"""

import base64
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8777
ROOT = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(ROOT, "secrets.json")

NEXUS_GRAPHQL = "https://api.nexusmods.com/v2/graphql"
NEXUS_V1 = "https://api.nexusmods.com/v1"
GAME_ID = "3024"                      # Red Dead Redemption 2 (verified via /v1/games.json)
GAME_DOMAIN = "reddeadredemption2"
GITHUB_API = "https://api.github.com"

SESSION_TOKEN = secrets.token_urlsafe(24)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def load_secrets():
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"[!] secrets.json unavailable ({exc}) - live features disabled")
        return {}


SECRETS = load_secrets()
NEXUS_KEY = SECRETS.get("nexus_api_key", "")
GITHUB_TOKEN = SECRETS.get("github_token", "")
GITHUB_REPO = SECRETS.get("github_repo", "meneedbot/rdr2-mods-from-nexus")


# ----------------------------------------------------------------- helpers
def nexus_headers():
    return {"apikey": NEXUS_KEY, "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "RDR2-Mod-Hub/1.0"}


def github_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RDR2-Mod-Hub/1.0"}


def graphql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(NEXUS_GRAPHQL, data=payload, headers=nexus_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"])[:400])
    return body.get("data", {})


# --------------------------------------------------------------- Nexus API
MOD_FIELDS = "id name summary downloads author"


def nexus_search(term, first=30, skip=0):
    """Keyword search over the RDR2 mod list (WILDCARD on the mod name)."""
    query = """
    query($q: String!, $n: Int!, $o: Int!) {
      mods(filter: { gameId: [{value: "%s"}], name: [{value: $q, op: WILDCARD}] },
           sort: [{downloads: {direction: DESC}}], count: $n, offset: $o) {
        nodes { %s }
      }
    }
    """ % (GAME_ID, MOD_FIELDS)
    data = graphql(query, {"q": term, "n": first, "o": skip})
    return (data.get("mods") or {}).get("nodes") or []


def nexus_sorted(sort_field="downloads", first=30, skip=0):
    """Browse the RDR2 mod list by a sort field (downloads / name / endorsements)."""
    if sort_field not in ("downloads", "name", "endorsements", "uniqueDownloads", "relevance"):
        sort_field = "downloads"
    query = """
    query($n: Int!, $o: Int!) {
      mods(filter: { gameId: [{value: "%s"}] },
           sort: [{ %s: {direction: DESC} }], count: $n, offset: $o) {
        nodes { %s }
      }
    }
    """ % (GAME_ID, sort_field, MOD_FIELDS)
    data = graphql(query, {"n": first, "o": skip})
    return (data.get("mods") or {}).get("nodes") or []


def nexus_mod_detail(mod_id):
    url = f"{NEXUS_V1}/games/{GAME_DOMAIN}/mods/{mod_id}.json"
    req = urllib.request.Request(url, headers=nexus_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# --------------------------------------------------------------- GitHub API
def _gh(path):
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}"


def gh_read(path):
    try:
        req = urllib.request.Request(_gh(path), headers=github_headers())
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        content = base64.b64decode(body.get("content", "")).decode("utf-8", "replace")
        return {"ok": True, "content": content, "url": body.get("html_url")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def gh_commit(path, text, message):
    """Create or update a file in the repo (PUT contents API, with sha retry)."""
    def _put(payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(_gh(path), data=data, headers=github_headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    payload = {"message": message,
               "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
               "branch": "main"}
    try:
        body = _put(payload)
        return {"ok": True, "created": True, "url": body.get("content", {}).get("html_url")}
    except urllib.error.HTTPError as exc:
        if exc.code == 422:                      # already exists -> update
            sha = None
            try:
                req = urllib.request.Request(_gh(path), headers=github_headers())
                with urllib.request.urlopen(req, timeout=25) as resp:
                    sha = json.loads(resp.read().decode("utf-8", "replace")).get("sha")
            except Exception:
                sha = None
            if sha:
                payload["sha"] = sha
                try:
                    body = _put(payload)
                    return {"ok": True, "created": False,
                            "url": body.get("content", {}).get("html_url")}
                except urllib.error.HTTPError as exc2:
                    return {"ok": False, "error": f"GitHub HTTP {exc2.code}: {exc2.read().decode('utf-8', 'replace')[:300]}"}
        return {"ok": False, "error": f"GitHub HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "RDR2ModHub/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            print("[api]", self.path)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel):
        rel = (rel or "/").lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(ROOT, rel))
        if not path.startswith(ROOT):
            return self._send(403, {"error": "forbidden"})
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ctype = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _session_ok(self):
        return self.headers.get("X-Session", "") == SESSION_TOKEN

    # ---------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/api/status":
            return self._send(200, {"session": SESSION_TOKEN,
                                    "nexus": bool(NEXUS_KEY),
                                    "github": bool(GITHUB_TOKEN),
                                    "repo": GITHUB_REPO})

        if path.startswith("/api/"):
            if not self._session_ok():
                return self._send(403, {"error": "session token required (open the page from this server)"})
            try:
                if path == "/api/nexus/search":
                    term = (q.get("q", [""])[0] or "").strip()
                    if not term:
                        return self._send(400, {"error": "empty query"})
                    return self._send(200, {"mods": nexus_search(
                        term, int(q.get("limit", ["30"])[0]), int(q.get("offset", ["0"])[0]))})
                if path == "/api/nexus/list":
                    return self._send(200, {"mods": nexus_sorted(
                        q.get("sort", ["downloads"])[0],
                        int(q.get("limit", ["30"])[0]), int(q.get("offset", ["0"])[0]))})
                if path.startswith("/api/nexus/mod/"):
                    return self._send(200, nexus_mod_detail(path.rsplit("/", 1)[-1]))
                if path == "/api/github/read":
                    return self._send(200, gh_read(q.get("path", ["mods.json"])[0]))
                if path == "/api/catalog":
                    return self._static("catalog.json")
            except urllib.error.HTTPError as exc:
                return self._send(exc.code, {"error": f"Nexus HTTP {exc.code}",
                                             "detail": exc.read().decode("utf-8", "replace")[:250]})
            except Exception as exc:
                return self._send(502, {"error": str(exc)})

        return self._static(path)

    # ---------------- POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            body = {}

        if parsed.path == "/api/github/commit":
            if not self._session_ok():
                return self._send(403, {"error": "session token required (open the page from this server)"})
            if not GITHUB_TOKEN:
                return self._send(400, {"error": "no GitHub token in secrets.json"})
            return self._send(200, gh_commit(
                body.get("path") or "mods.json",
                body.get("content") or "",
                body.get("message") or "Update mod list from RDR2 Mod Hub"))

        return self._send(404, {"error": "not found"})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 68)
    print("  RDR2 MOD HUB")
    print(f"  Site      : http://{HOST}:{PORT}/")
    print(f"  Nexus key : {'loaded' if NEXUS_KEY else 'MISSING'}  (live search)")
    print(f"  GitHub    : {'token loaded' if GITHUB_TOKEN else 'MISSING'} -> {GITHUB_REPO}")
    print(f"  Session   : {SESSION_TOKEN}")
    print("  Keys stay in secrets.json on this machine - never in the page or repo.")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.server_close()


if __name__ == "__main__":
    main()