"""Dashboard: a small authenticated web server on the standard library."""
import base64
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Signal Conductor</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#e4e2dc;--acc:#2b5fd9;--good:#1f8a4c;--bad:#c23b3b;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--mut:#9a988f;--card:#1f1f1d;--line:#34332f;--acc:#7da2ff;--good:#4cc27d;--bad:#ef6b6b;--warn:#e0a24a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:0 0 10px;color:var(--mut);font-weight:600;letter-spacing:.02em;text-transform:uppercase}
.sub{color:var(--mut);margin-bottom:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:10px;align-items:center;margin:6px 0}.lab{width:110px;color:var(--mut)}.bar{flex:1;height:12px;background:var(--line);border-radius:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc)}.num{width:56px;text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:600;font-size:13px}
.ok{color:var(--good)}.error{color:var(--bad)}.never{color:var(--warn)}.big{font-size:28px;font-weight:650}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:700px){.grid{grid-template-columns:1fr}}button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}
.scroll{overflow-x:auto}.mut{color:var(--mut)}svg{width:100%;height:90px}
</style></head><body><main>
<h1>Market Signal Conductor</h1><div class="sub" id="asof">loading…</div>
<div class="card"><h2>Market cycle</h2><div class="big" id="regime">–</div><div class="mut" id="pup"></div><div id="belief"></div>
<svg id="spark" viewBox="0 0 600 90" preserveAspectRatio="none"></svg></div>
<div class="card"><h2>Signals</h2><table id="signals"></table></div>
<div class="card"><h2>Strongest stocks</h2><div class="scroll"><table id="strong"></table></div></div>
<div class="card"><h2>Weakest stocks</h2><div class="scroll"><table id="weak"></table></div></div>
<div class="card"><h2>Track record and trading gate</h2><div id="track"></div><div id="gate"></div>
<p><button id="run">Run now</button> <span class="mut" id="runmsg"></span></p></div>
</main><script>
const $=id=>document.getElementById(id);
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e};
const pct=x=>x==null?'–':(100*x).toFixed(1)+'%';
function rows(tbl,head,data){tbl.replaceChildren();const h=el('tr');head.forEach(x=>h.append(el('th',x)));tbl.append(h);
 data.forEach(r=>{const tr=el('tr');r.forEach(c=>{const td=el('td');if(c instanceof Node)td.append(c);else td.textContent=c;tr.append(td)});tbl.append(tr)})}
async function load(){
 const r=await fetch('/api/state');const s=await r.json();
 $('asof').textContent=s.as_of?('As of '+s.as_of+' (UTC)'):'No run yet. Press "Run now".';
 $('regime').textContent=s.regime?s.regime.replace('_',' '):'–';
 $('pup').textContent=s.p_up==null?'':('Chance the market is up over the next '+s.horizon+' trading days: '+pct(s.p_up));
 const b=$('belief');b.replaceChildren();
 Object.entries(s.belief).forEach(([k,v])=>{const row=el('div',undefined,'row');row.append(el('span',k.replace('_',' '),'lab'));
  const bar=el('div',undefined,'bar');const i=el('i');i.style.width=(100*v)+'%';bar.append(i);row.append(bar);row.append(el('span',pct(v),'num'));b.append(row)});
 const h=s.history;const sp=$('spark');sp.replaceChildren();
 if(h.length>1){const pts=h.map((d,i)=>(600*i/(h.length-1)).toFixed(1)+','+(88-80*d.p_up).toFixed(1)).join(' ');
  const ns='http://www.w3.org/2000/svg';const pl=document.createElementNS(ns,'polyline');pl.setAttribute('points',pts);pl.setAttribute('fill','none');pl.setAttribute('stroke','currentColor');pl.setAttribute('stroke-width','2');sp.append(pl)}
 rows($('signals'),['Signal','Weight','Track record','Status'],s.signals.map(x=>{
  const st=el('span',x.health.status,x.health.status==='ok'?'ok':(x.health.status==='error'?'error':'never'));
  const wrap=el('div');wrap.append(st);if(x.health.detail)wrap.append(el('div',x.health.detail,'mut'));
  const tr=x.pairs>=12?('corr '+(x.corr==null?'–':x.corr.toFixed(2))+' over '+x.pairs+' outcomes'):('too early ('+x.pairs+' outcomes)');
  const d=el('div');d.append(el('strong',x.name));d.append(el('div',x.description,'mut'));
  return [d,x.weight.toFixed(2),tr,wrap]}));
 const t=l=>l.map(x=>[x.ticker,x.company||'–',x.industry||'–',pct(x.p_strong),pct(x.p_weak),(x.evidence||[]).join(', ')]);
 const H=['Ticker','Company','Industry','P(strong)','P(weak)','Evidence'];
 rows($('strong'),H,t(s.tilts.strongest));rows($('weak'),H,t(s.tilts.weakest));
 const k=s.track;$('track').textContent=k.n?('Scored forecasts: '+k.n+'. Hit rate '+pct(k.hit_rate)+'. Brier '+k.brier.toFixed(3)+' vs base rate '+k.brier_base.toFixed(3)+'.'):'No forecasts scored yet (each needs '+s.horizon+' trading days to resolve).';
 $('gate').textContent=s.gate.passed?'Gate: PASSED. Live trading code may be considered.':('Gate: closed. '+s.gate.reasons.join('; ')+'.');
}
$('run').onclick=async()=>{$('runmsg').textContent='running…';const r=await fetch('/api/run',{method:'POST'});$('runmsg').textContent=r.ok?'done':'failed';load()};
load();setInterval(load,60000);
</script></body></html>"""


def make_handler(conductor, cfg, run_lock):
    expected = None
    if cfg.dash_password:
        expected = base64.b64encode(f"{cfg.dash_user}:{cfg.dash_password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self):
            if expected is None:
                return True
            got = self.headers.get("Authorization", "")
            return got.startswith("Basic ") and hmac.compare_digest(got[6:], expected)

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _deny(self):
            self._send(401, "Authentication required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="Market Signal Conductor"'})

        def do_GET(self):
            if self.path == "/healthz":                    # no auth: health checks and uptime monitors; shows only the last run day
                try:
                    last = conductor.store.last_belief()
                    last_run = last[0] if last else "never"
                except Exception:
                    last_run = "unknown"
                return self._send(200, f"ok last_run={last_run}", "text/plain")
            if not self._authed():
                return self._deny()
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE)
            if self.path == "/api/state":
                return self._send(200, json.dumps(conductor.state()), "application/json")
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._authed():
                return self._deny()
            if self.path == "/api/run":
                if not run_lock.acquire(blocking=False):
                    return self._send(409, "a run is already in progress", "text/plain")
                try:
                    conductor.run_day()
                finally:
                    run_lock.release()
                return self._send(200, json.dumps({"ok": True}), "application/json")
            self._send(404, "not found", "text/plain")

    return Handler


def serve(conductor, cfg, run_lock=None, host="0.0.0.0"):
    if not cfg.dash_password and not cfg.allow_open_dashboard:
        raise SystemExit("Set MSC_DASH_PASSWORD before serving on the web (or MSC_ALLOW_OPEN=1 for local testing).")
    run_lock = run_lock or threading.Lock()
    server = ThreadingHTTPServer((host, cfg.port), make_handler(conductor, cfg, run_lock))
    return server
