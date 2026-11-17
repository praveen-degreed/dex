"""The Dex page on http://127.0.0.1:<port>: what is running, spend and tokens, and the controls (stop, run a review,
restart, pause, switches, caps, Claude accounts).

It is its own process, bound to loopback only, and refuses any Host header that is not 127.0.0.1 or localhost on its
own port (so a web page cannot reach it through DNS rebinding). Reads need nothing more. Every change (POST, and the
PR preview) also needs:
  - an Origin header of this page itself (a cross-site form or fetch carries the other site's origin), and
  - the page's per-process token in an X-Dex-Token header (a custom header, so a cross-site request cannot send it
    without a CORS preflight, which this server never answers), and a JSON body.

Settings are written to policy.json through the same code as `dexctl` (so the same validation applies), and the bot
picks them up without a restart. Stop, run and restart go to the bot through control/inbox/ (see dex/control.py).
Each change is appended to audit.jsonl with source "page".
"""
import contextlib
import datetime
import hmac
import io
import json
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import claude_env, config, control, ctl, github, people
from .policy import ALL

PAGE = (Path(__file__).resolve().parent / "dashboard.html").read_text()
MAX_BODY = 16 * 1024
KINDS = ("review", "question", "router")

HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                      "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'"}


def _audit(**fields):
    try:
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "user": config.OWNER, "capability": "control",
               "source": "page", **fields}
        with (config.DATA / "audit.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _ctl(*argv):
    """Run a dexctl command in-process. Returns (ok, its output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = ctl.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc == 0, out.getvalue().strip()


def settings():
    cfg = ctl.load_json(ctl.POLICY, {})
    mj = cfg.get("max_job_usd")
    if not isinstance(mj, dict):
        mj = {"review": mj, "question": mj} if mj else {}
    return {"paused": bool(cfg.get("paused")), "disabled": sorted(set(cfg.get("disabled") or [])),
            "capabilities": ["review", "question"],
            "caps": {"review": mj.get("review"), "question": mj.get("question"),
                     "daily": (cfg.get("global") or {}).get("daily_usd"), "pr_files": cfg.get("max_pr_files")},
            "accounts": {k: ctl._c_profile(cfg, k) for k in KINDS}, "profiles": sorted(claude_env.profiles())}


def today_counts(audit):
    day = datetime.date.today().isoformat()
    rows = [r for r in audit if (r.get("ts") or "").startswith(day)]
    return {"reviews": sum(1 for r in rows if r.get("capability") == "review" and r.get("decision") == "done"),
            "questions": sum(1 for r in rows if r.get("capability") == "question" and r.get("decision") == "answered")}


def state_payload():
    d = ctl.collect(7)
    d["settings"] = settings()
    d["today"] = today_counts(ctl.read_audit(1))
    d["names"] = {sid: (p or {}).get("name") or sid for sid, p in people.all_people().items()}
    d["repos"] = sorted(config.REPOS)
    d["owner"] = config.OWNER
    rt = d.get("runtime") or {}
    d["controls_ready"] = bool(d["running"]) and "control" in rt  # an older bot without the control thread ignores stop/run
    return d


def pr_preview(url):
    m = github.PR_URL.match((url or "").strip())
    if not m:
        return 400, {"error": "Not a GitHub pull request link."}
    r = subprocess.run(["gh", "pr", "view", m.group(0), "--json",
                        "number,title,author,additions,deletions,changedFiles,state,isDraft,headRefOid,baseRefName"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode:
        return 502, {"error": (r.stderr.strip() or "gh pr view failed")[:200]}
    info = json.loads(r.stdout)
    repo = m.group(1).lower()
    cfg = config.REPOS.get(repo)
    return 200, {"url": m.group(0), "repo": repo, "number": info.get("number"), "title": info.get("title"),
                 "author": (info.get("author") or {}).get("login"), "additions": info.get("additions"),
                 "deletions": info.get("deletions"), "files": info.get("changedFiles"), "state": info.get("state"),
                 "draft": info.get("isDraft"), "sha": (info.get("headRefOid") or "")[:7],
                 "reviewable": bool(cfg), "skill": (cfg or {}).get("skill", "").split()[0] if cfg else None}


def _amount(v):
    if v in (None, "", "off"):
        return "off"
    return str(float(v))


def apply_action(body):
    """One change from the page. Returns (http status, payload)."""
    a = body.get("action")
    if a in ("pause", "resume"):
        ok, out = _ctl(a)
    elif a in ("enable", "disable"):
        cap = body.get("capability")
        if cap not in ALL:
            return 400, {"ok": False, "message": "Unknown capability."}
        ok, out = _ctl(a, cap)
    elif a == "cap":
        what, value = body.get("what"), _amount(body.get("value"))
        if what in ("review", "question"):
            ok, out = _ctl("cap", "job-usd", what, value)
        elif what == "daily":
            ok, out = _ctl("cap", "daily-usd", value)
        elif what == "pr_files":
            ok, out = _ctl("cap", "pr-files", value if value == "off" else str(int(float(value))))
        else:
            return 400, {"ok": False, "message": "Unknown limit."}
    elif a == "account":
        kind, prof = body.get("kind"), body.get("profile")
        if kind not in KINDS or prof not in claude_env.profiles():
            return 400, {"ok": False, "message": "Unknown kind of run or account."}
        ok, out = _ctl("claude", "use", kind, prof)
    elif a == "check_account":
        prof = body.get("profile")
        if prof not in claude_env.profiles():
            return 400, {"ok": False, "message": "Unknown account."}
        ok, out = _ctl("claude", "check", prof)
        return 200, {"ok": ok, "message": out.splitlines()[-1] if out else ("usable" if ok else "not usable")}
    elif a in control.ACTIONS:
        args = {k: body[k] for k in ("job", "url", "force") if k in body}
        cid = control.submit(a, args)
        res = control.result(cid, wait=20 if a == "run" else 6)
        if res is None:
            return 202, {"ok": None, "id": cid, "message": "Sent; Dex has not answered yet."}
        return 200, res
    else:
        return 400, {"ok": False, "message": "Unknown action."}
    _audit(decision=f"page_{a}", ok=ok, target=body.get("capability") or body.get("what") or body.get("kind"),
           **{k: body[k] for k in ("value", "profile") if k in body})
    msg = "Saved." if ok else (out.splitlines()[-1] if out else "Failed.")
    return (200 if ok else 400), {"ok": ok, "message": msg.replace("error: ", "")}


def make_handler(port_getter, token):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def _origins(self):
            port = port_getter()
            return (f"127.0.0.1:{port}", f"localhost:{port}")

        def host_ok(self):
            return (self.headers.get("Host") or "") in self._origins()

        def change_ok(self):
            """Host, Origin and token: only this page, loaded from this server, can change anything."""
            if not self.host_ok():
                return False
            if (self.headers.get("Origin") or "") not in tuple("http://" + o for o in self._origins()):
                return False
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
                return False
            return hmac.compare_digest(self.headers.get("X-Dex-Token") or "", token)

        def do_GET(self):
            if not self.host_ok():
                return self._send(403, "forbidden")
            parts = urlsplit(self.path)
            if parts.path == "/":
                return self._send(200, PAGE.replace("__DEX_TOKEN__", token), "text/html; charset=utf-8")
            if parts.path == "/api/state":
                return self._json(200, state_payload())
            if parts.path == "/api/pr":
                if not self.change_ok():
                    return self._send(403, "forbidden")
                try:
                    code, obj = pr_preview((parse_qs(parts.query).get("url") or [""])[0])
                except Exception as e:
                    code, obj = 502, {"error": str(e)[:200]}
                return self._json(code, obj)
            if parts.path == "/api/result":
                if not self.change_ok():
                    return self._send(403, "forbidden")
                res = control.result((parse_qs(parts.query).get("id") or [""])[0])
                return self._json(200, res or {"ok": None})
            return self._send(404, "not found")

        def do_POST(self):
            if not self.change_ok():
                return self._send(403, "forbidden")
            if urlsplit(self.path).path != "/api/action":
                return self._send(404, "not found")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._send(415, "send JSON")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                return self._send(413, "bad length")
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, "bad JSON")
            if not isinstance(body, dict):
                return self._send(400, "bad JSON")
            try:
                code, obj = apply_action(body)
            except Exception as e:
                code, obj = 500, {"ok": False, "message": f"Failed: {str(e)[:200]}"}
            return self._json(code, obj)

        def _no(self):
            self._send(405, "not allowed")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

        def log_message(self, *a):
            pass

    return Handler


def make_server(port, token=None):
    holder = {}
    token = token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lambda: holder["port"], token))
    holder["port"] = server.server_address[1]
    server.dex_token = token
    return server


def serve(port):
    server = make_server(port)
    print(f"Dex page: http://127.0.0.1:{server.server_address[1]}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
