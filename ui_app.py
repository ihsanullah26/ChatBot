#!/usr/bin/env python3
"""
Simple browser UI wrapper for watchman_chatbox_postgres(1).py.

IMPORTANT: The existing chatbot logic is NOT copied or modified here.
This file imports the existing answer_question() function and only adds
an HTTP/browser interface around it.

>>> SECURITY NOTE (read before exposing this beyond localhost):
ppcode_usersregister.password stores UNSALTED MD5 hashes — a legacy
scheme from the existing production app, not something introduced
here. This file authenticates against that scheme because that's
what the real accounts actually use; it does NOT re-hash anything
with a stronger algorithm, since that would lock out every real user
until they reset their password. MD5 is fast to crack (rainbow
tables, GPU brute-force) and unsalted means identical passwords
produce identical hashes across accounts. This is a real weakness in
the existing system, not just this demo — worth raising with whoever
owns the production login flow. Practical implications for this file:
  - Sessions are a plain in-memory dict — lost on restart, and not
    safe for multiple server processes/workers.
  - There is NO rate-limiting or lockout on failed login attempts.
  - Keep this on localhost (or behind your own auth) unless those
    gaps are addressed — the tunnel script in this project exposes
    it publicly over HTTPS, which protects the wire but does nothing
    about the two points above.
"""

import hashlib
import http.cookies
import json
import importlib.util
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Find the user's existing chatbot file without changing its source code.
# Your terminal command shows the file is named watchman_chatbox_postgres.py,
# while the uploaded copy was named watchman_chatbox_postgres(1).py.
CHATBOT_CANDIDATES = [
    BASE_DIR / "watchman_chatbox_postgres.py",
    BASE_DIR / "watchman_chatbox_postgres(1).py",
]

CHATBOT_FILE = next((p for p in CHATBOT_CANDIDATES if p.is_file()), None)

if CHATBOT_FILE is None:
    names = ", ".join(p.name for p in CHATBOT_CANDIDATES)
    raise FileNotFoundError(
        f"Could not find the existing chatbot code in {BASE_DIR}. "
        f"Expected one of: {names}"
    )

# Load the user's existing chatbot module without changing its source code.
spec = importlib.util.spec_from_file_location("watchman_chatbot", CHATBOT_FILE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load chatbot module: {CHATBOT_FILE}")
chatbot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chatbot)

HOST = "0.0.0.0"
PORT = 8000

# Only this exact account is treated as an administrator. The password is
# never hardcoded here; login still validates it against the existing
# ppcode_usersregister record.
ADMIN_EMAIL = "704ballyshannondr@gmail.com"

# ============================================================
# SESSIONS
# ------------------------------------------------------------
# Plain in-memory dict: session token -> {"user_id", "name", "created"}.
# Deliberately simple for a single-process local demo — see the
# SECURITY NOTE at the top of this file for what that means in
# practice if this ever runs somewhere less controlled.
# ============================================================

SESSIONS = {}
SESSION_COOKIE_NAME = "watchman_session"
SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours


def _new_session(user_id: str, name: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    normalized_email = email.strip().lower()
    SESSIONS[token] = {
        "user_id": user_id, "name": name, "email": normalized_email,
        "is_admin": normalized_email == ADMIN_EMAIL, "created": time.time(),
    }
    return token


def _lookup_session(token: str):
    if not token:
        return None
    session = SESSIONS.get(token)
    if not session:
        return None
    if time.time() - session["created"] > SESSION_TTL_SECONDS:
        SESSIONS.pop(token, None)
        return None
    return session


# ============================================================
# HTML — LOGIN PAGE
# ============================================================

LOGIN_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — Watchman</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#10202e; --ink-deep:#17324a;
  --paper:#eef1f4; --surface:#ffffff;
  --slate:#1e2b38; --slate-muted:#62748a;
  --line:#d7dee6;
  --lantern:#d98c2b; --lantern-deep:#b96f17; --lantern-soft:#d98c2b22;
  --danger:#b3413a;
  --sans:"IBM Plex Sans",-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  --serif:"IBM Plex Serif",Georgia,serif;
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:var(--sans);background:linear-gradient(160deg,var(--ink) 0%,var(--ink-deep) 100%);color:var(--slate);display:flex;align-items:center;justify-content:center;-webkit-font-smoothing:antialiased}
.card{width:100%;max-width:520px;background:var(--surface);border-radius:18px;box-shadow:0 24px 60px #0006;padding:56px 50px;margin:20px}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:34px}
.lamp{width:13px;height:13px;border-radius:50%;background:var(--lantern);box-shadow:0 0 0 4px var(--lantern-soft);flex:none}
.title{font-family:var(--serif);font-size:27px;font-weight:600;color:var(--ink)}
h1{font-size:20px;font-weight:600;margin:0 0 7px;color:var(--slate)}
.sub{font-size:14.5px;color:var(--slate-muted);margin:0 0 32px}
label{display:block;font-size:14px;color:var(--slate-muted);font-weight:500;margin:0 0 8px}
input{width:100%;padding:15px 17px;border:1px solid var(--line);border-radius:10px;font:inherit;font-size:16px;color:var(--slate);margin-bottom:22px;outline:none}
input:focus{border-color:var(--lantern);box-shadow:0 0 0 3px var(--lantern-soft)}
button{width:100%;border:0;border-radius:10px;background:var(--lantern);color:#221304;font-weight:600;font-size:16.5px;padding:16px;cursor:pointer;transition:background .15s}
button:hover:not(:disabled){background:var(--lantern-deep)}
button:disabled{opacity:.6;cursor:not-allowed}
.error{background:#b3413a14;border:1px solid #b3413a3a;color:var(--danger);font-size:13.5px;border-radius:9px;padding:12px 14px;margin-bottom:20px;display:none}
.error.show{display:block}
.pw-wrap{position:relative;margin-bottom:22px}
.pw-wrap input{margin-bottom:0;padding-right:46px}
button.pw-toggle{position:absolute;top:0;right:0;width:46px;height:100%;padding:0;border:0;background:transparent;color:var(--slate-muted);display:flex;align-items:center;justify-content:center;cursor:pointer}
button.pw-toggle:hover:not(:disabled){background:transparent;color:var(--ink)}
button.pw-toggle svg{width:20px;height:20px;pointer-events:none}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><span class="lamp" aria-hidden="true"></span><span class="title">Watchman</span></div>
  <h1>Sign in to your account</h1>
  <p class="sub">Use your Watchman portal email and password.</p>
  <div id="err" class="error"></div>
  <form id="loginForm">
    <label for="email">Email</label>
    <input id="email" type="email" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <div class="pw-wrap">
      <input id="password" type="password" autocomplete="current-password" required>
      <button type="button" id="togglePw" class="pw-toggle" aria-label="Show password">
        <svg id="eyeShow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>
        <svg id="eyeHide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a21.6 21.6 0 0 1 5.06-6.06M9.9 4.24A10.4 10.4 0 0 1 12 4c7 0 11 7 11 7a21.6 21.6 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>
      </button>
    </div>
    <button id="submitBtn" type="submit">Sign in</button>
  </form>
</div>
<script>
const form = document.getElementById('loginForm');
const err = document.getElementById('err');
const btn = document.getElementById('submitBtn');
const pwInput = document.getElementById('password');
const togglePw = document.getElementById('togglePw');
const eyeShow = document.getElementById('eyeShow');
const eyeHide = document.getElementById('eyeHide');
togglePw.addEventListener('click', () => {
  const show = pwInput.type === 'password';
  pwInput.type = show ? 'text' : 'password';
  eyeShow.style.display = show ? 'none' : '';
  eyeHide.style.display = show ? '' : 'none';
  togglePw.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
});
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.classList.remove('show');
  btn.disabled = true;
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('email').value.trim(),
        password: document.getElementById('password').value,
      }),
    });
    const data = await r.json();
    if (r.ok) {
      window.location.href = data.admin ? '/admin' : '/';
    } else {
      err.textContent = data.error || 'Sign in failed.';
      err.classList.add('show');
    }
  } catch (e) {
    err.textContent = 'Could not reach the server.';
    err.classList.add('show');
  } finally {
    btn.disabled = false;
  }
});
</script>
</body></html>'''


# ============================================================
# HTML — CHAT UI
# ============================================================

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watchman Chatbot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#10202e; --ink-deep:#17324a;
  --paper:#eef1f4; --surface:#ffffff;
  --slate:#1e2b38; --slate-muted:#62748a;
  --line:#d7dee6;
  --lantern:#d98c2b; --lantern-deep:#b96f17; --lantern-soft:#d98c2b22;
  --signal:#3e8e5b;
  --sans:"IBM Plex Sans",-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  --serif:"IBM Plex Serif",Georgia,serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;font-family:var(--sans);background:var(--paper);color:var(--slate);-webkit-font-smoothing:antialiased}

.header{background:linear-gradient(160deg,var(--ink) 0%,var(--ink-deep) 100%);color:#fff;padding:26px 0;position:relative}
.header::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;background:linear-gradient(90deg,var(--lantern) 0%,transparent 65%)}
.header-inner{width:90%;max-width:1400px;margin:0 auto;padding:0 4px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:14px}
.lamp{width:11px;height:11px;border-radius:50%;background:var(--lantern);box-shadow:0 0 0 4px var(--lantern-soft);flex:none;animation:glow 2.6s ease-in-out infinite}
@keyframes glow{0%,100%{box-shadow:0 0 0 4px var(--lantern-soft)}50%{box-shadow:0 0 0 7px var(--lantern-soft)}}
.title{font-family:var(--serif);font-size:23px;font-weight:600;letter-spacing:.2px}
.subtitle{font-size:13px;color:#c7d3de;margin-top:3px}
.who{display:flex;align-items:center;gap:14px}
.who span{font-size:13px;color:#dbe4ec}
.logout{border:1px solid #ffffff30;background:transparent;color:#dbe4ec;padding:7px 14px;border-radius:999px;font-size:12.5px;cursor:pointer;font-family:inherit}
.logout:hover{border-color:var(--lantern);color:#fff}

.wrap{width:90%;max-width:1400px;margin:30px auto;padding:0 4px}

.config{display:flex;gap:22px;margin-bottom:16px;align-items:center;flex-wrap:wrap;padding:14px 18px;background:var(--surface);border:1px solid var(--line);border-radius:10px}
.config label{font-size:13px;color:var(--slate-muted);display:flex;align-items:center;gap:8px;font-weight:500}
.config select{min-width:220px;max-width:440px;width:auto;padding:8px 12px;border:1px solid var(--line);border-radius:7px;font:inherit;font-size:13.5px;background:var(--surface);color:var(--slate);text-overflow:ellipsis}
.config select:disabled{background:#f2f4f6;color:#a4b0bd}
.config select:focus{outline:none;border-color:var(--lantern);box-shadow:0 0 0 3px var(--lantern-soft)}
.config .note{font-size:12.5px;color:var(--slate-muted);margin-left:auto}

.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 32px #17324a0f;overflow:hidden}
.chat{height:min(600px,65vh);overflow-y:auto;padding:26px;background:var(--surface)}

.empty-state{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px;color:var(--slate-muted)}
.empty-icon{width:40px;height:40px;color:var(--lantern);margin-bottom:16px}
.empty-state h2{font-family:var(--serif);font-size:19px;font-weight:600;color:var(--slate);margin:0 0 6px}
.empty-state p{font-size:13.5px;margin:0 0 22px;max-width:380px}
.chips{display:flex;flex-wrap:wrap;justify-content:center;gap:9px;max-width:560px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--slate);border-radius:999px;padding:9px 16px;font:inherit;font-size:13px;cursor:pointer;transition:border-color .15s,color .15s}
.chip:hover{border-color:var(--lantern);color:var(--lantern-deep)}
.msg{display:flex;margin:0 0 20px}.msg.user{justify-content:flex-end}
.msg>div{max-width:72%;min-width:0}
.bubble{padding:13px 17px;border-radius:12px;line-height:1.55;font-size:14.5px;white-space:pre-wrap;word-wrap:break-word;min-width:96px}
.user .bubble{background:var(--ink);color:#f3f6f9;border-bottom-right-radius:4px}
.bot .bubble{background:#f6f8fa;color:var(--slate);border:1px solid var(--line);border-bottom-left-radius:4px}
.bot .bubble a{color:var(--lantern-deep);text-decoration:underline}.bot .bubble a:hover{color:var(--lantern)}
.meta{font-size:11.5px;color:var(--slate-muted);margin:0 0 6px 3px;font-family:var(--mono)}
.user .meta{text-align:right;margin-right:3px}

.composer{padding:18px;border-top:1px solid var(--line);background:var(--surface)}
.row{display:flex;gap:10px}
#question{flex:1;min-height:46px;max-height:160px;border:1px solid var(--line);border-radius:9px;padding:12px 15px;font:inherit;font-size:14.5px;color:var(--slate);outline:none;resize:none;overflow-y:auto;line-height:1.4;background:#fbfcfd}
#question:focus{border-color:var(--lantern);box-shadow:0 0 0 3px var(--lantern-soft);background:var(--surface)}
button{border:0;border-radius:9px;background:var(--lantern);color:#221304;font-weight:600;padding:0 26px;cursor:pointer;font-size:14.5px;transition:background .15s}
button:hover:not(:disabled){background:var(--lantern-deep)}
button:disabled{opacity:.5;cursor:not-allowed}
.help{font-size:12px;color:var(--slate-muted);margin-top:10px}
.typing{font-style:italic;color:var(--slate-muted)}

@media(max-width:700px){
  .wrap{width:94%}.header-inner{width:94%;align-items:flex-start;flex-direction:column;gap:12px}
  .msg>div{max-width:88%}.row{flex-direction:column}button{height:46px}
  .config .note{margin-left:0}
}
</style>
</head>
<body>
<header class="header"><div class="header-inner">
<div class="brand">
  <span class="lamp" aria-hidden="true"></span>
  <div><div class="title">Watchman</div><div class="subtitle">Unit support &amp; sensor history</div></div>
</div>
<div class="who"><span id="whoami"></span><a id="adminLink" class="logout" href="/admin" style="display:none;text-decoration:none">Admin history</a><button id="logoutBtn" class="logout" type="button">Log out</button></div>
</div></header>
<main class="wrap">
<div class="config">
<label>Unit <select id="unitId" disabled><option value="">Loading units…</option></select></label>
<span class="note">Loaded from your Watchman account.</span>
</div>
<section class="card">
<div id="chat" class="chat">
<div id="emptyState" class="empty-state">
  <svg class="empty-icon" viewBox="0 0 48 48" fill="none" aria-hidden="true">
    <circle cx="24" cy="24" r="21" stroke="currentColor" stroke-width="2" opacity=".35"/>
    <circle cx="24" cy="24" r="7.5" fill="currentColor"/>
  </svg>
  <h2>Ask about a unit, or about Watchman itself</h2>
  <p>Pick a unit above, then try one of these — or type your own.</p>
  <div class="chips">
    <button type="button" class="chip">What's the current temperature?</button>
    <button type="button" class="chip">What was the max temperature this week?</button>
    <button type="button" class="chip">How do I reconnect my WiFi?</button>
    <button type="button" class="chip">What are supported APIs?</button>
  </div>
</div>
</div>
<div class="composer"><div class="row"><textarea id="question" rows="1" placeholder="Example: How do I rename my LTE modem?" autofocus></textarea><button id="send">Send</button></div><div class="help">Press Enter to send.</div></div>
</section>
</main>
<script>
const chat=document.getElementById('chat');
const emptyState=document.getElementById('emptyState');
const q=document.getElementById('question');
const send=document.getElementById('send');
const unitId=document.getElementById('unitId');
const whoami=document.getElementById('whoami');
const logoutBtn=document.getElementById('logoutBtn');
const adminLink=document.getElementById('adminLink');

// Auto-grow the question box as text wraps to more lines, instead of
// scrolling a fixed-height single line sideways (which made typed text
// appear to "shift left" as a longer question grew past the visible
// width). Height resets to min-height on each input so deleting text
// shrinks it back down too, not just grows.
function autoResizeQuestion(){
 q.style.height = 'auto';
 q.style.height = Math.min(q.scrollHeight, 160) + 'px';
}
q.addEventListener('input', autoResizeQuestion);

// Escape raw HTML first so nothing in the model's output can inject markup
// or scripts, THEN convert markdown-style [label](url) links (and any
// bare http(s) URLs the model might emit without brackets) into real
// clickable <a> tags that open in a new tab. This only applies to bot
// messages — user-typed text stays as plain textContent, no parsing needed.
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
function linkify(escapedText) {
  // Markdown links: [label](https://example.com)
  let out = escapedText.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  // Fallback: any remaining bare URL not already inside an href="...".
  // By this point escapeHtml() has already turned a literal "<" into
  // "&lt;", so a placeholder address like "http://<IP address>/<Watchman
  // ID>&Stats" appears here as "http://&lt;IP address&gt;/...". A real
  // URL never legitimately contains a raw "<", so the (?!&lt;) guard
  // stops the match right before it — which for a placeholder means the
  // match fails entirely (no partial link like "http://<IP" left
  // dangling with the rest of the address as unlinked plain text).
  out = out.replace(
    /(?<!href=")(https?:\/\/(?:(?!&lt;)[^\s])+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  return out;
}
// The model sometimes emits markdown emphasis (**bold**) which, left as
// literal asterisks, showed up as raw "**" in the chat bubble. Convert
// it to real <strong> tags — same escape-first-then-format approach as
// linkify(), so nothing in the model's output can inject markup.
function boldify(escapedText) {
  return escapedText.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function addMessage(role,text){
 if(emptyState && emptyState.parentNode) emptyState.remove();
 const wrap=document.createElement('div'); wrap.className='msg '+role;
 const inner=document.createElement('div');
 const meta=document.createElement('div'); meta.className='meta'; meta.textContent=role==='user'?'You':'Watchman Assistant';
 const bubble=document.createElement('div'); bubble.className='bubble';
 if(role==='bot'){
   bubble.innerHTML=linkify(boldify(escapeHtml(text)));
 } else {
   bubble.textContent=text;
 }
 inner.appendChild(meta); inner.appendChild(bubble); wrap.appendChild(inner); chat.appendChild(wrap); chat.scrollTop=chat.scrollHeight;
 return wrap;
}

function syncSelectTitle(selectEl){
 const opt = selectEl.options[selectEl.selectedIndex];
 selectEl.title = opt ? opt.textContent : '';
}

async function loadWhoAmI(){
 const r = await fetch('/api/me');
 if(r.status === 401){ window.location.href = '/login'; return; }
 const data = await r.json();
 whoami.textContent = data.name ? `Signed in as ${data.name}` : '';
 if(data.admin) adminLink.style.display = 'inline-block';
}

// --- Unit dropdown, populated for the LOGGED-IN user only (server
// derives the user from the session cookie — never trusts a client-
// supplied user_id) ---
async function loadUnits(){
 unitId.disabled = true;
 unitId.innerHTML = '<option value="">Loading units…</option>';
 try{
   const r = await fetch('/api/units');
   if(r.status === 401){ window.location.href = '/login'; return; }
   const data = await r.json();
   unitId.innerHTML = '';
   if(!data.units || !data.units.length){
     unitId.innerHTML = '<option value="">No units on this account</option>';
     return;
   }
   for(const u of data.units){
     const opt = document.createElement('option');
     opt.value = u.unit_id;
     opt.textContent = u.unit_name
       ? `${u.unit_name} — #${u.unit_id} (${u.unit_type})`
       : `Unit #${u.unit_id} (${u.unit_type})`;
     unitId.appendChild(opt);
   }
   unitId.disabled = false;
   syncSelectTitle(unitId);
 }catch(e){
   unitId.innerHTML = '<option value="">Could not load units</option>';
 }
}
unitId.addEventListener('change', ()=>syncSelectTitle(unitId));
loadWhoAmI();
loadUnits();

async function sendQuestion(){
 const question=q.value.trim(); if(!question||send.disabled)return;
 if(!unitId.value){ addMessage('bot','Pick a unit above first.'); return; }
 addMessage('user',question); q.value=''; autoResizeQuestion(); send.disabled=true;
 const typing=addMessage('bot','Thinking...'); typing.querySelector('.bubble').classList.add('typing');
 try{
   const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,unit_id:Number(unitId.value)})});
   if(r.status === 401){ window.location.href = '/login'; return; }
   const data=await r.json(); typing.remove();
   addMessage('bot',data.answer||data.error||'No response returned.');
 }catch(e){typing.remove();addMessage('bot','Could not reach the chatbot server. Check the terminal where the app is running.');}
 finally{send.disabled=false;q.focus();}
}
send.addEventListener('click',sendQuestion);
q.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();sendQuestion();}});

document.querySelectorAll('.chip').forEach(chip=>{
  chip.addEventListener('click', ()=>{ q.value = chip.textContent; sendQuestion(); });
});

logoutBtn.addEventListener('click', async ()=>{
  await fetch('/logout', {method:'POST'});
  window.location.href = '/login';
});
</script>
</body></html>'''



# ============================================================
# HTML — ADMIN HISTORY
# ============================================================

ADMIN_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watchman — Admin History</title>
<style>
body{margin:0;background:#eef1f4;color:#1e2b38;font-family:-apple-system,"Segoe UI",Arial,sans-serif}
.header{background:linear-gradient(160deg,#10202e,#17324a);color:#fff;padding:24px 4%}.head{max-width:1500px;margin:auto;display:flex;justify-content:space-between;align-items:center;gap:15px}
.title{font:600 23px Georgia,serif}.sub{font-size:13px;color:#c7d3de;margin-top:3px}.actions{display:flex;gap:8px}.actions a,.actions button{border:1px solid #ffffff40;background:transparent;color:#fff;border-radius:999px;padding:8px 14px;text-decoration:none;cursor:pointer}
.wrap{width:92%;max-width:1500px;margin:28px auto}.card{background:#fff;border:1px solid #d7dee6;border-radius:12px;overflow:hidden}.toolbar{padding:16px 18px;border-bottom:1px solid #d7dee6;display:flex;gap:12px;align-items:center}.toolbar h1{font-size:18px;margin:0}.count,.status{color:#62748a;font-size:13px}.toolbar button{margin-left:auto;background:#d98c2b;border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer}.status{padding:10px 18px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:1050px}th,td{text-align:left;vertical-align:top;border-top:1px solid #d7dee6;padding:12px;font-size:13px}th{background:#f6f8fa;color:#62748a;white-space:nowrap}.question,.answer{white-space:pre-wrap}.question{min-width:250px;max-width:430px}.answer{min-width:330px;max-width:560px}.parsed{font:11.5px ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;max-width:330px;word-break:break-word}.empty{text-align:center;padding:35px;color:#62748a}
.cell-rest.cell-full{display:none}
.expand-toggle{display:inline-block;margin-top:6px;background:none;border:0;color:#d98c2b;font:600 12px inherit;cursor:pointer;padding:0}
.expand-toggle:hover{color:#b96f17;text-decoration:underline}
</style></head><body>
<header class="header"><div class="head"><div><div class="title">Watchman Admin</div><div class="sub">Customer question &amp; response history</div></div><div class="actions"><a href="/">Open Chat</a><button id="logout">Log out</button></div></div></header>
<main class="wrap"><section class="card"><div class="toolbar"><h1>Chat history</h1><span id="count" class="count"></span><button id="refresh">Refresh</button></div><div id="status" class="status">Loading…</div>
<div class="table-wrap"><table><thead><tr><th>Time (UTC)</th><th>User ID</th><th>Unit</th><th>Customer question</th><th>Watchman response</th><th>Parsed query</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" style="display:none">No chat logs found yet.</div></div>
</section></main>
<script>
const rows=document.getElementById('rows'),statusEl=document.getElementById('status'),countEl=document.getElementById('count'),empty=document.getElementById('empty');
const esc=v=>String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');

// Collapses any cell whose content runs more than 5 rows past its first
// line (this is what happens for count_threshold_crossings answers with
// hundreds/thousands of matching readings — see the bug this fixes).
// The header line (e.g. "Temperature went below 70 3790 time(s)...")
// always stays visible; only the bulleted rows beneath it collapse.
// Both the collapsed and full versions are rendered up front (one
// hidden via CSS) rather than stored in a data attribute, so there's
// no re-escaping/quoting to get wrong when the toggle fires.
function renderCell(text, cls, maxExtraRows=5){
  const escaped = esc(text);
  const lines = escaped.split('\n');
  if(lines.length <= 1 + maxExtraRows){
    return `<div class="${cls}">${escaped}</div>`;
  }
  const head = lines[0];
  const collapsedRest = lines.slice(1, 1 + maxExtraRows).join('\n');
  const fullRest = lines.slice(1).join('\n');
  const hiddenCount = lines.length - 1 - maxExtraRows;
  return `<div class="${cls}">${head}\n<span class="cell-rest cell-collapsed" style="display:inline">${collapsedRest}</span><span class="cell-rest cell-full" style="display:none">${fullRest}</span>
<button type="button" class="expand-toggle" data-state="collapsed" data-collapsed-label="Show ${hiddenCount} more row(s)" data-expanded-label="Show fewer rows">Show ${hiddenCount} more row(s)</button></div>`;
}

async function loadLogs(){statusEl.textContent='Loading…';rows.innerHTML='';try{const r=await fetch('/api/admin/logs');if(r.status===401){location.href='/login';return}if(r.status===403){statusEl.textContent='Admin access required.';return}const d=await r.json();if(!r.ok)throw Error(d.error||'Could not load logs.');countEl.textContent=`${d.logs.length} record(s)`;if(!d.logs.length){empty.style.display='block';statusEl.textContent='';return}rows.innerHTML=d.logs.map(x=>`<tr><td>${esc(x.timestamp)}</td><td>${esc(x.user_id)}</td><td>#${esc(x.unit_id)}</td><td>${renderCell(x.question,'question')}</td><td>${renderCell(x.answer,'answer')}</td><td>${renderCell(JSON.stringify(x.parsed??null,null,2),'parsed')}</td></tr>`).join('');statusEl.textContent='Showing the most recent 500 records.'}catch(e){statusEl.textContent=e.message||'Could not load logs.'}}

// Delegated click handler — rows are re-rendered wholesale on every
// loadLogs() call, so binding once on the (stable) tbody instead of
// per-button avoids rebinding listeners on every refresh.
rows.addEventListener('click', (e)=>{
  const btn = e.target.closest('.expand-toggle');
  if(!btn) return;
  const expanded = btn.dataset.state === 'expanded';
  const collapsedEl = btn.parentElement.querySelector('.cell-collapsed');
  const fullEl = btn.parentElement.querySelector('.cell-full');
  if(expanded){
    collapsedEl.style.display = 'inline';
    fullEl.style.display = 'none';
    btn.textContent = btn.dataset.collapsedLabel;
    btn.dataset.state = 'collapsed';
  } else {
    collapsedEl.style.display = 'none';
    fullEl.style.display = 'inline';
    btn.textContent = btn.dataset.expandedLabel;
    btn.dataset.state = 'expanded';
  }
});

document.getElementById('refresh').onclick=loadLogs;document.getElementById('logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.href='/login'};loadLogs();
</script></body></html>'''
class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="text/html; charset=utf-8", extra_headers=None):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for header, value in (extra_headers or []):
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for header, value in (extra_headers or []):
            self.send_header(header, value)
        self.end_headers()

    def _get_session(self):
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        jar = http.cookies.SimpleCookie()
        jar.load(raw_cookie)
        morsel = jar.get(SESSION_COOKIE_NAME)
        if not morsel:
            return None
        return _lookup_session(morsel.value)

    def _session_cookie_header(self, token, max_age=None):
        # HttpOnly (JS can't read/steal it via XSS) + SameSite=Lax (basic
        # CSRF mitigation for a same-site app). Not marking Secure since
        # this runs over plain HTTP on localhost by default — if you
        # tunnel this over HTTPS, browsers will still accept a non-
        # Secure cookie over TLS, so this doesn't need to change for that.
        parts = [f"{SESSION_COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        return "; ".join(parts)

    def do_GET(self):
        session = self._get_session()

        if self.path == "/login":
            if session:
                self._redirect("/")
                return
            self._send(200, LOGIN_HTML)
            return

        if self.path == "/" or self.path.startswith("/?"):
            if not session:
                self._redirect("/login")
                return
            self._send(200, HTML)
            return

        if self.path == "/api/me":
            if not session:
                self._send(401, json.dumps({"error": "Not signed in."}), "application/json")
                return
            self._send(
                200,
                json.dumps({"user_id": session["user_id"], "name": session["name"],
                            "email": session.get("email", ""),
                            "admin": bool(session.get("is_admin"))}),
                "application/json",
            )
            return

        if self.path == "/api/units":
            if not session:
                self._send(401, json.dumps({"error": "Not signed in."}), "application/json")
                return
            self._handle_get_units(session["user_id"])
            return

        if self.path == "/admin":
            if not session:
                self._redirect("/login")
                return
            if not session.get("is_admin"):
                self._send(403, "Admin access required.", "text/plain; charset=utf-8")
                return
            self._send(200, ADMIN_HTML)
            return

        if self.path == "/api/admin/logs":
            if not session:
                self._send(401, json.dumps({"error": "Not signed in."}), "application/json")
                return
            if not session.get("is_admin"):
                self._send(403, json.dumps({"error": "Admin access required."}), "application/json")
                return
            self._handle_admin_logs()
            return

        self._send(404, "Not found", "text/plain; charset=utf-8")

    def _handle_get_units(self, user_id: str):
        # The units a given user actually owns. There's no direct
        # "owner" column on watchrecord — alertconfigure(user_id,
        # splog_id) is the real link between a user and the unit(s)
        # they've configured alerts for (splog_id == watchrecord.wid,
        # i.e. our unit_id), joined to watchrecord to get the unit's
        # type for display. DISTINCT because a unit can have more than
        # one alertconfigure row (config history) over time.
        #
        # PII note (previously flagged when this was scoped to a single
        # demo account via DEMO_USER_ID_FILTER): unit display names
        # come from the sensor table's own `address` column, which for
        # most real units is a literal customer street address, not a
        # nickname. That's no longer a demo-only workaround — real
        # login now means a user can only ever see their OWN units'
        # addresses, the same way they could through the real portal.
        try:
            with chatbot.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                            "SELECT DISTINCT ac.splog_id, wr.type "
                            "FROM alertconfigure ac "
                            "JOIN watchrecord wr ON wr.wid = ac.splog_id "
                            "WHERE ac.user_id = %s "
                            "ORDER BY ac.splog_id",
                            (user_id,),
                )
                    
                    unit_rows = cur.fetchall()

                    # All distinct sensor tables across UNIT_TYPE_TABLE_MAP,
                    # primary-table-first order doesn't matter here since we
                    # try the unit's own mapped table first anyway below.
                    ALL_SENSOR_TABLES = sorted({
                        cfg["table"] for cfg in chatbot.UNIT_TYPE_TABLE_MAP.values()
                    })

                    def _lookup_name(unit_id, primary_table):
                        # Some real units' data lives in a different table
                        # than their nominal `type` maps to (confirmed
                        # directly against the dump — e.g. a handful of
                        # 'simple' units actually log to
                        # logofwatchwithrealy instead of avpeopletable).
                        # Try the correctly-mapped table first (if any —
                        # primary_table is None for a unit whose `type`
                        # isn't in UNIT_TYPE_TABLE_MAP at all), then fall
                        # back through the others rather than leaving a
                        # unit nameless just because of a type/table
                        # mismatch, or an unmapped type, elsewhere in the
                        # data.
                        tables_to_try = ([primary_table] if primary_table else []) + [
                            t for t in ALL_SENSOR_TABLES if t != primary_table
                        ]
                        for table in tables_to_try:
                            try:
                                cur.execute(
                                    f"SELECT address FROM {table} "
                                    f"WHERE splog_id = %s "
                                    f"ORDER BY datetime DESC LIMIT 1",
                                    (unit_id,),
                                )
                                row = cur.fetchone()
                                if row and row[0]:
                                    return row[0]
                            except Exception:
                                continue
                        return None

                    units = []
                    for unit_id, unit_type in unit_rows:
                        table_config = chatbot.UNIT_TYPE_TABLE_MAP.get(unit_type)
                        primary_table = table_config["table"] if table_config else None
                        unit_name = _lookup_name(unit_id, primary_table)
                        units.append({"unit_id": unit_id, "unit_type": unit_type, "unit_name": unit_name})
            self._send(200, json.dumps({"units": units}), "application/json")
        except Exception as exc:
            print(f"[UI error] /api/units {type(exc).__name__}: {exc}")
            self._send(500, json.dumps({"error": "Could not load units."}), "application/json")

    def do_POST(self):
        if self.path == "/login":
            self._handle_login()
            return
        if self.path == "/logout":
            self._handle_logout()
            return
        if self.path == "/chat":
            self._handle_chat()
            return
        self._send(404, json.dumps({"error": "Not found"}), "application/json")

    def _handle_login(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            email = str(payload.get("email", "")).strip()
            password = str(payload.get("password", ""))
            if not email or not password:
                self._send(400, json.dumps({"error": "Email and password are required."}), "application/json")
                return

            # >>> SECURITY NOTE: this compares against the existing
            # UNSALTED MD5 scheme already used by ppcode_usersregister —
            # see the module docstring at the top of this file. Not
            # something to copy into a new system.
            password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()

            with chatbot.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT user_id, name, password FROM ppcode_usersregister "
                        "WHERE LOWER(email) = LOWER(%s)",
                        (email,),
                    )
                    row = cur.fetchone()

            # Deliberately the same generic error whether the email
            # doesn't exist or the password is wrong — don't reveal
            # which one to an attacker probing for valid emails.
            if not row or row[2] != password_hash:
                self._send(401, json.dumps({"error": "Invalid email or password."}), "application/json")
                return

            user_id, name, _ = row
            token = _new_session(user_id, name, email)
            is_admin = email.strip().lower() == ADMIN_EMAIL
            self._send(
                200,
                json.dumps({"ok": True, "admin": is_admin}),
                "application/json",
                extra_headers=[("Set-Cookie", self._session_cookie_header(token, max_age=SESSION_TTL_SECONDS))],
            )
        except Exception as exc:
            print(f"[UI error] /login {type(exc).__name__}: {exc}")
            self._send(500, json.dumps({"error": "Login failed. Check the server terminal for details."}), "application/json")

    def _handle_logout(self):
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            jar = http.cookies.SimpleCookie()
            jar.load(raw_cookie)
            morsel = jar.get(SESSION_COOKIE_NAME)
            if morsel:
                SESSIONS.pop(morsel.value, None)
        self._send(
            200,
            json.dumps({"ok": True}),
            "application/json",
            extra_headers=[("Set-Cookie", self._session_cookie_header("", max_age=0))],
        )

    def _handle_admin_logs(self):
        """Read chat_logs.jsonl and expose only recent records to the admin."""
        log_path = Path(chatbot.CHAT_LOG_PATH)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        logs = []
        try:
            if log_path.is_file():
                with log_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            record = json.loads(line)
                            if isinstance(record, dict):
                                logs.append({
                                    "timestamp": record.get("timestamp", ""),
                                    "user_id": record.get("user_id", ""),
                                    "unit_id": record.get("unit_id", ""),
                                    "question": record.get("question", ""),
                                    "parsed": record.get("parsed"),
                                    "answer": record.get("answer", ""),
                                })
                        except (json.JSONDecodeError, TypeError):
                            continue
            logs = logs[-500:]
            logs.reverse()
            self._send(200, json.dumps({"logs": logs}, ensure_ascii=False), "application/json")
        except OSError as exc:
            print(f"[UI error] /api/admin/logs {type(exc).__name__}: {exc}")
            self._send(500, json.dumps({"error": "Could not read chat history."}), "application/json")

    def _handle_chat(self):
        session = self._get_session()
        if not session:
            self._send(401, json.dumps({"error": "Not signed in."}), "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(payload.get("question", "")).strip()
            # user_id ALWAYS comes from the session, never from the
            # request body — otherwise a signed-in user could simply
            # edit the payload to query someone else's units. unit_id
            # is still checked against unit_belongs_to_user() downstream
            # in the chatbot itself, which is the real enforcement point.
            user_id = session["user_id"]
            unit_id = int(payload.get("unit_id", chatbot.UNIT_ID))
            if not question:
                raise ValueError("Question cannot be empty.")

            answer = chatbot.answer_question(user_id=user_id, unit_id=unit_id, question=question)
            self._send(200, json.dumps({"answer": answer}), "application/json")
        except Exception as exc:
            print(f"[UI error] /chat {type(exc).__name__}: {exc}")
            self._send(500, json.dumps({"error": "The chatbot could not process the request. Check the server terminal for details."}), "application/json")

    def log_message(self, fmt, *args):
        print("[HTTP] " + fmt % args)


if __name__ == "__main__":
    print(f"[Watchman UI] http://{HOST}:{PORT}  (redirects to /login if not signed in)")
    print("[Watchman UI] Existing chatbot logic is loaded unchanged.")
    print("[Watchman UI] Press Ctrl+C to stop.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Watchman UI] Stopped.")
    finally:
        server.server_close()
