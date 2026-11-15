"""A read-only dashboard for Dex on http://127.0.0.1:<port>: what is running, spend and tokens, the most expensive
actions, and the switches. It reads the same files as `dexctl` and changes nothing; use `dexctl` to change switches.

It is its own process, bound to loopback only, answers GET only, and refuses any Host header that is not
127.0.0.1 or localhost on its own port (so a web page cannot reach it through DNS rebinding).
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ctl

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Dex</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#8b93a7;--ok:#3ecf8e;--warn:#f5a623;--bad:#ef5350}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:14px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:0 0 8px}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;padding:16px 20px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto}
.wide{grid-column:1/-1}table{border-collapse:collapse;width:100%}td,th{padding:4px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:500}.dim{color:var(--dim)}.pill{padding:2px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.num{text-align:right;font-variant-numeric:tabular-nums}
</style></head><body>
<header><h1>Dex</h1><span id="state" class="pill">loading</span><span id="sw" class="dim"></span><span class="dim" style="margin-left:auto" id="upd"></span></header>
<main>
<section class="wide"><h2>Running now</h2><div id="running"></div></section>
<section><h2>Today</h2><div id="today"></div></section>
<section><h2>Last 7 days by capability</h2><div id="cap"></div></section>
<section><h2>Last 7 days by person</h2><div id="user"></div></section>
<section><h2>Last 7 days by day</h2><div id="day"></div></section>
<section class="wide"><h2>Most expensive actions (7 days)</h2><div id="top"></div></section>
<section class="wide"><h2>Recent events</h2><div id="recent"></div></section>
<section class="wide"><h2>Switches and caps</h2><div id="caps"></div><p class="dim">Read-only. Change them with <code>dexctl pause | disable review | cap job-usd review 4 | cap pr-files 80</code>.</p></section>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e};
const human=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"K":String(Math.round(n||0));
const age=s=>{s=Math.max(0,Math.round(s));return s>=3600?Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m":s>=60?Math.floor(s/60)+"m "+s%60+"s":s+"s"};
const tk=t=>{t=t||{};const sum=(t.fresh||0)+(t.cache_read||0)+(t.output||0);return human(sum)+" tok (out "+human(t.output||0)+")"};
function table(target,cols,rows,empty){const box=$(target);box.replaceChildren();if(!rows.length){box.append(el("div",empty||"nothing","dim"));return}
 const t=el("table"),h=el("tr");cols.forEach(c=>h.append(el("th",c[0],c[2])));t.append(h);
 rows.forEach(r=>{const tr=el("tr");cols.forEach(c=>tr.append(el("td",String(c[1](r)),c[2])));t.append(tr)});box.append(t)}
const usd=v=>"$"+(v||0).toFixed(2);
async function tick(){let d;try{d=await (await fetch("/api/state",{cache:"no-store"})).json()}catch(e){$("state").textContent="dashboard cannot reach its data";return}
 const rt=d.runtime,pol=rt&&rt.policy;const s=$("state");
 if(!d.running){s.textContent="Dex not running";s.className="pill bad"}else if(pol.paused){s.textContent="PAUSED";s.className="pill warn"}else{s.textContent="running";s.className="pill ok"}
 $("sw").textContent=pol&&pol.disabled.length?"switched off: "+pol.disabled.join(", "):"";
 $("upd").textContent=rt?"updated "+age(d.last_seen_s)+" ago":"";
 table("running",[["what",r=>r.kind+" "+(r.what||"")+(r.sha?" @"+r.sha:"")],["who",r=>r.user||"-"],["status",r=>r.status],["for",r=>age(r.age_s),"num"],["steps",r=>r.steps,"num"],["tokens",r=>tk(r.tokens)],["doing",r=>r.activity||""]],rt?rt.running:[],"nothing running");
 const u=pol?pol.users:{};table("today",[["person",r=>r[0]],["role",r=>r[1].role],["$",r=>usd(r[1].usd),"num"],["tokens",r=>tk(r[1].tokens)]],Object.entries(u).sort((a,b)=>b[1].usd-a[1].usd),"no requests yet today");
 if(pol){const t=$("today");t.append(el("p","Total today "+usd(pol.global_usd)+", "+tk(pol.tokens)+", "+pol.router_calls+" router calls","dim"))}
 const rows=o=>Object.entries(o).sort((a,b)=>b[1].usd-a[1].usd);const cols=[["",r=>r[0]],["$",r=>usd(r[1].usd),"num"],["tokens",r=>human(r[1].tokens),"num"],["actions",r=>r[1].n,"num"]];
 table("cap",cols,rows(d.by_capability));table("user",cols,rows(d.by_user));table("day",cols,Object.entries(d.by_day).reverse());
 table("top",[["$",r=>usd(r.usd),"num"],["when",r=>(r.ts||"").slice(0,16)],["what",r=>r.capability+" / "+r.decision],["who",r=>r.user||""],["target",r=>r.pr||r.repo||""],["tokens",r=>r.tokens?tk(r.tokens):""]],d.top);
 table("recent",[["when",r=>(r.ts||"").slice(5,19)],["who",r=>r.user||""],["what",r=>(r.capability||"")+" / "+(r.decision||"")],["$",r=>r.usd?usd(r.usd):"","num"],["target",r=>r.pr||r.repo||r.error||""]],d.recent);
 if(pol){const c=[["paused",pol.paused?"YES":"no"],["switched off",pol.disabled.join(", ")||"none"],["per-job cap",JSON.stringify(pol.max_job_usd||"none")],["PR files limit",pol.max_pr_files||"none"],["daily total cap",(pol.global&&pol.global.daily_usd)||"none"]];
  table("caps",[["setting",r=>r[0]],["value",r=>r[1]]],c)}}
tick();setInterval(tick,3000);
</script></body></html>"""

HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}


def make_handler(port_getter):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            port = port_getter()
            return (self.headers.get("Host") or "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.host_ok():
                return self._send(403, "forbidden")
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, json.dumps(ctl.collect(7)), "application/json")
            return self._send(404, "not found")

        def _no(self):
            self._send(405, "this dashboard is read-only")

        do_POST = do_PUT = do_DELETE = do_PATCH = _no

        def log_message(self, *a):
            pass

    return Handler


def make_server(port):
    holder = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lambda: holder["port"]))
    holder["port"] = server.server_address[1]
    return server


def serve(port):
    server = make_server(port)
    print(f"Dex dashboard (read-only): http://127.0.0.1:{server.server_address[1]}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
