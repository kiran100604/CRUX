#!/usr/bin/env python3
"""Assemble a 'raw screen recording' style demo that uses CRUX's REAL components:
the actual <style> + <nav> from crux/static/index.html, and the real Working-view
markup classes — so the dashboard in the video is the genuine product UI. Adds a
faithful Claude Code terminal (matching the app screenshot) and animates the loop.
"""
import re, pathlib

src = pathlib.Path("crux/static/index.html").read_text(encoding="utf-8")
# tokens now live in a managed file; inline them so the demo is self-contained
tokens = pathlib.Path("crux/static/tokens.css").read_text(encoding="utf-8")
style = tokens + "\n" + src[src.index("<style>")+7 : src.index("</style>")]
nav = src[src.index("<nav>") : src.index("</nav>")+6]
# open the app on the Working tab, with realistic badge counts
nav = nav.replace('class="tab on" data-v="overview"', 'class="tab" data-v="overview"')
nav = nav.replace('class="tab" data-v="working"', 'class="tab on" data-v="working"')
nav = nav.replace('id="t-kb">0', 'id="t-kb">24')
nav = nav.replace('id="t-wm">0', 'id="t-wm">1')
nav = nav.replace('id="t-dec">0', 'id="t-dec">3')

# ---- the REAL Working/thread markup (same classes the app renders) ----
def card(kind, label, text):
    return f'''<div class="dumpcard demo-hide" data-card>
      <div class="dcside"><button class="incl on">✓</button></div>
      <div class="dcmain">
        <div class="dctop"><span class="kind k-{kind}">{label}</span><span class="dcsrc">via claude-code</span><span class="iwhen mono">just now</span></div>
        <div class="dctext">{text}</div>
      </div>
      <button class="dcdel">×</button>
    </div>'''

THREAD = f'''
    <div class="crumb"><span class="bk">← Working</span> <b>//</b> Data sync feature</div>
    <div class="threadhd">
      <div><h1 class="head display" style="font-size:30px">Data sync feature</h1></div>
      <div class="thactions">
        <button class="btn teal">⧉ Copy context</button>
        <button class="btn out sm">for a task…</button>
        <button class="btn out sm">Finish</button>
      </div>
    </div>

    <div class="intpanel">
      <div class="ctxhd"><span class="intlbl">★ INTENT — what you're working toward</span></div>
      <textarea id="intbox" class="intbox" readonly>Build a fast data-sync feature</textarea>
    </div>

    <div class="ctxpanel">
      <div class="ctxhd">
        <span class="ctxlbl">◆ WORKING MEMORY — decisions, current state &amp; what you've learned</span>
        <span class="ctxtag"><span class="autotag">auto</span></span>
        <span class="resum">↻ refine</span>
      </div>
      <textarea id="ctxbox" class="ctxbox"></textarea>
    </div>

    <div class="dumprow">
      <input class="ntinput" placeholder="Dump anything — a prompt, a link, a result, a chunk of chat, an idea…"/>
      <button class="btn teal">Dump →</button>
    </div>
    <div class="hotkeyhint">⌨ <b>⌃⇧Space</b> dumps straight here from anywhere</div>

    <div class="dumphd" id="dumphd">DUMPS · 3 · 3 in context</div>
    <div class="dumplist" id="dumplist">
      {card("decision","Decision","Sync incrementally, not full-table")}
      {card("constraint","Constraint","Never deploy on Fridays")}
      {card("question","Open question","How should we resolve write conflicts?")}
    </div>
'''

WM_TEXT = ("Sync layer is incremental (not full-table) for speed. Friday deploys are "
           "off-limits. Conflict-resolution strategy still open.")

EXTRA_CSS = '''
  /* ---------- demo chrome: full-screen "apps", cursor, captions ---------- */
  html{height:100%}
  body{padding-top:26px; overflow-y:auto}
  body::-webkit-scrollbar{display:none}
  .menubar{position:fixed; top:0; left:0; right:0; height:26px; z-index:200;
    background:rgba(20,18,14,.82); backdrop-filter:blur(8px); display:flex; align-items:center;
    gap:18px; padding:0 16px; color:#e9e2d2; font-size:12.5px; font-family:var(--sans)}
  .menubar .ap{font-weight:700} .menubar .mr{margin-left:auto; display:flex; gap:16px; opacity:.9}
  /* the CRUX app is the real page; keep its sticky nav just below the menubar */
  nav{top:26px}
  .wrap{padding-top:16px}
  /* focus the Working view so the memory panel + captured cards sit on one screen */
  .crumb,.dumprow,.hotkeyhint,.bgwrap,.seswrap,.promote{display:none}
  #ctxbox{min-height:78px}
  .threadhd{margin-bottom:10px}

  /* terminal app — full-screen overlay matching the Claude Code window */
  .termapp{position:fixed; inset:0; z-index:150; background:#080706;
    display:flex; align-items:center; justify-content:center; padding:60px 40px 40px;
    transition:opacity .55s ease; }
  .termapp.hide{opacity:0; pointer-events:none}
  .ccwin{width:min(1180px,92vw); height:min(78vh,820px); background:#100f0d; border-radius:14px;
    overflow:hidden; box-shadow:0 40px 120px rgba(0,0,0,.6); display:flex; flex-direction:column;
    border:1px solid #2a2620}
  .ccbar{position:relative; display:flex; align-items:center; height:42px; padding:0 16px;
    background:#15130f; border-bottom:1px solid #262119}
  .tl{width:13px;height:13px;border-radius:50%;margin-right:9px}
  .tl.r{background:#ff5f57}.tl.y{background:#febc2e}.tl.g{background:#28c840}
  .cct{position:absolute; left:0; right:0; text-align:center; color:#b8b1a2; font-size:14px;
    font-family:var(--sans); pointer-events:none}
  .ccblue{position:absolute; top:0; right:0; width:240px; height:3px; background:#3b82f6}
  .ccbody{flex:1; min-height:0; padding:22px 26px; font-family:var(--mono); font-size:16px;
    line-height:1.62; color:#e7ddc6; overflow:hidden; display:flex; flex-direction:column}
  .ccpath{border:1px solid #6a5436; color:#cdbf9f; border-radius:8px; padding:10px 18px; margin-bottom:18px}
  .ccscr{flex:1; min-height:0; white-space:pre-wrap; word-break:break-word}
  .ccscr .g{color:#6fc0a8}
  .ccscr .hl{background:rgba(255,255,255,.06); display:block; margin:0 -10px; padding:2px 10px}
  .ccscr .agent{color:#e7ddc6}
  .ccscr .agent b{color:#e7ddc6}
  .ccscr .name{background:#7a1d1d; color:#ffd9d4; padding:0 4px; border-radius:3px}
  .ccscr .tool b{color:#e7ddc6}
  .ccscr .dim{color:#857d6c}
  .ccscr .ok{color:#5fb98a}
  .ccscr .out{color:#cfe6dd}
  .ccscr .ctx{display:block; margin:6px 0 4px; padding:12px 14px; border:1px dashed #3a5650;
    border-radius:8px; color:#9fcabb; font-size:14.5px; line-height:1.55}
  .ccscr .ctx b{color:#cdeadf}
  .ccscr .hook{color:#6f9e90}
  .ccspin{color:#d98a3d; margin-top:10px}
  .ccin{margin-top:14px; border-top:1px solid #2a2620; padding-top:12px; color:#cfe6dd}
  .cblock{display:inline-block;width:9px;height:18px;background:#cfe6dd;vertical-align:-3px;animation:bl 1s steps(1) infinite}
  @keyframes bl{50%{opacity:0}}
  .ccesc{color:#6b6353; margin-top:10px; font-size:14px}

  /* fake cursor */
  .cursor{position:fixed; z-index:300; width:22px; height:22px; pointer-events:none;
    transition:left .85s cubic-bezier(.5,.05,.2,1), top .85s cubic-bezier(.5,.05,.2,1); left:60%; top:60%}
  .cursor.click{animation:clk .35s ease}
  @keyframes clk{50%{transform:scale(.8)}}

  /* tiny subtitle (kept minimal for the raw feel) */
  .sub{position:fixed; left:50%; bottom:42px; transform:translateX(-50%) translateY(8px); z-index:210;
    background:rgba(8,8,8,.82); color:#fff; font-family:var(--sans); font-size:16px; font-weight:500;
    padding:11px 20px; border-radius:999px; opacity:0; transition:opacity .4s, transform .4s; max-width:80vw; text-align:center}
  .sub.show{opacity:1; transform:translateX(-50%)}

  /* reveal animation for the real dump cards */
  .dumpcard{transition:opacity .5s ease, transform .5s ease}
  .dumpcard.demo-hide{opacity:0; transform:translateX(26px)}
  .ctxpanel.flash{box-shadow:0 0 0 3px rgba(31,157,87,.35)}

  .replay{position:fixed; inset:0; display:none; place-items:center; background:rgba(8,8,8,.8); z-index:400}
  .replay.show{display:grid}
  .replay button{font-family:var(--sans); font-size:20px; font-weight:600; color:#fff; background:var(--teal);
    border:none; border-radius:999px; padding:16px 38px; cursor:pointer}
'''

TERM_HTML = '''
<div class="termapp" id="termapp">
  <div class="ccwin">
    <div class="ccbar"><span class="tl r"></span><span class="tl y"></span><span class="tl g"></span>
      <span class="cct">📁 · Claude Code</span><span class="ccblue"></span></div>
    <div class="ccbody">
      <div class="ccpath" id="ccpath">~/sync-service</div>
      <div class="ccscr" id="ccscr"></div>
      <div class="ccin"><span class="g">›</span> <span class="cblock"></span></div>
      <div class="ccesc">esc to interrupt</div>
    </div>
  </div>
</div>
'''

CURSOR = '''<div class="cursor" id="cursor"><svg viewBox="0 0 24 24" width="22" height="22">
  <path d="M5 3l14 8-6 1.4L9.6 19 5 3z" fill="#fff" stroke="#111" stroke-width="1"/></svg></div>'''

JS = r'''
<script>
const $=id=>document.getElementById(id);
const termapp=$('termapp'), ccscr=$('ccscr'), ccpath=$('ccpath'), cursor=$('cursor'),
      sub=$('sub'), ctxbox=$('ctxbox'), replay=$('replay');
let RUN=0;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function cap(t){ sub.textContent=t||''; sub.classList.toggle('show', !!t); }
function moveCursor(x,y){ cursor.style.left=x; cursor.style.top=y; }
function click(){ cursor.classList.remove('click'); void cursor.offsetWidth; cursor.classList.add('click'); }

async function typeInto(el, text, cps, id){
  for(const ch of text){ if(id!==RUN) return; el.textContent+=ch; await sleep(1000/cps); }
}
async function typeVal(el, text, cps, id){
  el.value=''; for(const ch of text){ if(id!==RUN) return; el.value+=ch; el.scrollTop=el.scrollHeight; await sleep(1000/cps); }
}
function add(html){ const d=document.createElement('div'); d.innerHTML=html; ccscr.appendChild(d); return d; }
async function stream(el, text, wps, id){
  const w=text.split(' '); for(let i=0;i<w.length;i++){ if(id!==RUN)return; el.textContent+=(i?' ':'')+w[i]; await sleep(1000/wps); }
}

function resetCrux(){
  document.querySelectorAll('[data-card]').forEach(c=>c.classList.add('demo-hide'));
  ctxbox.value='';
}
function reset(){
  RUN++; ccscr.innerHTML=''; termapp.classList.remove('hide'); resetCrux();
  cap(''); replay.classList.remove('show'); moveCursor('62%','64%');
}

async function play(){
  reset(); const id=RUN;
  ccpath.textContent='~/sync-service';

  /* ---- ACT 1 — coding in Claude Code ---- */
  await sleep(500); if(id!==RUN)return;
  const p1=add('<span class="hl"><span class="g">›</span> </span>');
  const sp1=p1.querySelector('.hl');
  await typeInto(sp1,'implement the incremental sync layer',30,id); if(id!==RUN)return;
  cap('You just code — normally.'); await sleep(500); if(id!==RUN)return;

  add('<div style="height:8px"></div>');
  add('<span class="agent">● <b>build-agent</b> (implement incremental sync)</span>'); await sleep(450); if(id!==RUN)return;
  add('<span class="tool">  └ <b>Read</b>(src/sync.ts)</span>'); await sleep(350); if(id!==RUN)return;
  add('<span class="tool">  <b>Edit</b>(src/sync.ts)</span>'); await sleep(350); if(id!==RUN)return;
  add('<span class="tool">  <b>Bash</b>(npm test) <span class="ok">✓ 12 passing</span></span>'); await sleep(550); if(id!==RUN)return;
  add('<div style="height:8px"></div>');
  const o1=add('<span class="out"></span>').querySelector('.out');
  await stream(o1,'Going with incremental sync over full-table — much faster. Skipping Friday deploys per your constraint. Open question: how should we resolve write conflicts?',8,id);
  if(id!==RUN)return;
  await sleep(500); if(id!==RUN)return;
  add('<div style="height:10px"></div>');
  add('<span class="hook">⌁ crux · captured 3 signals → working memory</span>');
  cap('CRUX captures from the agent automatically — through hooks. No new step.');
  await sleep(1900); if(id!==RUN)return;

  /* ---- switch to the real CRUX dashboard ---- */
  moveCursor('50%','40%'); await sleep(700); if(id!==RUN)return; click();
  termapp.classList.add('hide');
  cap('The actual CRUX dashboard — your working memory, filling itself.');
  window.scrollTo(0,0);
  await sleep(800); if(id!==RUN)return;
  // reveal the captured cards one by one
  const cards=[...document.querySelectorAll('[data-card]')];
  for(let i=0;i<cards.length;i++){ cards[i].classList.remove('demo-hide'); moveCursor('46%', (52+i*7)+'%'); await sleep(650); if(id!==RUN)return; }
  document.querySelector('.ctxpanel').classList.add('flash');
  await typeVal(ctxbox, ''' + repr(WM_TEXT) + r''', 40, id); if(id!==RUN)return;
  document.querySelector('.ctxpanel').classList.remove('flash');
  cap('Decisions, constraints, open questions — tagged “via claude-code.”');
  await sleep(2600); if(id!==RUN)return;

  /* ---- ACT 3 — next session, context auto-injected ---- */
  resetCrux();
  termapp.classList.remove('hide'); ccscr.innerHTML='';
  ccpath.textContent='~/sync-service'; cap('Next morning. A brand-new session.');
  moveCursor('60%','62%');
  await sleep(900); if(id!==RUN)return;
  const ctx=add('<span class="ctx"></span>').querySelector('.ctx');
  ctx.innerHTML='<b>⌁ CRUX context — auto-injected at session start</b>\n';
  await typeInto(ctx,'INTENT: build a fast data-sync feature\nDECISIONS: incremental sync · Postgres · no Friday deploys\nOPEN: write-conflict resolution',95,id);
  if(id!==RUN)return;
  cap('CRUX hands the agent exactly where you left off.'); await sleep(700); if(id!==RUN)return;
  add('<div style="height:8px"></div>');
  const p2=add('<span class="hl"><span class="g">›</span> </span>').querySelector('.hl');
  await typeInto(p2,'continue where we left off',30,id); if(id!==RUN)return;
  await sleep(400); if(id!==RUN)return;
  add('<div style="height:8px"></div>');
  const o2=add('<span class="out"></span>').querySelector('.out');
  await stream(o2,'Resuming the incremental Postgres sync. Next up: write-conflict resolution — last-writer-wins with a version clock.',8,id);
  if(id!==RUN)return;
  cap('It already knew. No re-explaining. No repeated mistakes.');
  await sleep(3200); if(id!==RUN)return;

  cap('CRUX — local-first memory for your coding agents.');
  await sleep(2600); if(id!==RUN)return;
  replay.classList.add('show');
}
window.play=play;
play();
</script>
'''

MENUBAR = ('<div class="menubar"><span class="ap">Claude Code</span>'
           '<span>File</span><span>Edit</span><span>View</span>'
           '<span class="mr"><span>CRUX ⌁</span><span>100%</span><span>9:41 AM</span></span></div>')

html = (
    "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\"/>\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>\n"
    "<title>CRUX — product demo</title>\n<style>" + style + EXTRA_CSS + "</style>\n</head>\n<body>\n"
    + MENUBAR + "\n"
    + nav + "\n"
    + '<div id="app" class="wrap">' + THREAD + "</div>\n"
    + TERM_HTML + "\n"
    + CURSOR + "\n"
    + '<div class="sub" id="sub"></div>\n'
    + '<div class="replay" id="replay"><button onclick="play()">▶ Replay</button></div>\n'
    + JS + "\n</body>\n</html>\n"
)
pathlib.Path("docs/demo.html").write_text(html, encoding="utf-8")
print("wrote docs/demo.html", len(html), "bytes")
