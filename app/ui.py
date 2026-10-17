# -*- coding: utf-8 -*-
"""内置聊天页 /ui + 会话记忆。

- 服务端按 sid 记录上下文（sqlite，data/gateway.db 的 messages 表），
  客户端每轮只发新消息，不再重发全部历史；
- 拼上下文时自动截断：默认只带最近 12 条（policy.session_max_messages 可调），
  避免多轮对话把免费额度吃光；
- 每条回复展示：实际路由到的上游/模型、是否免费、prompt/completion tokens、耗时。
"""
from __future__ import annotations

import json
import sqlite3
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from . import config, store, upstream, pricing
from . import router as _routing
from . import memory as _memory

router = APIRouter()

DB = config.DATA_DIR / "gateway.db"


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sid TEXT NOT NULL, ts REAL NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL,
        model TEXT DEFAULT '', tokens INTEGER DEFAULT 0)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_sid ON messages(sid, id)")
    return conn


def add(sid: str, role: str, content: str, model: str = "", tokens: int = 0) -> None:
    with _db() as c:
        c.execute("INSERT INTO messages(sid, ts, role, content, model, tokens) VALUES(?,?,?,?,?,?)",
                  (sid, time.time(), role, content, model, tokens))


def history(sid: str, limit: int) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE sid=? ORDER BY id DESC LIMIT ?",
            (sid, limit)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def clear(sid: str) -> int:
    with _db() as c:
        n = c.execute("DELETE FROM messages WHERE sid=?", (sid,)).rowcount
    return n


def _window(sid: str, cfg: dict) -> list[dict]:
    pol = cfg.get("policy") or {}
    try:
        limit = max(2, int(pol.get("session_max_messages") or 12))
    except Exception:
        limit = 12
    return history(sid, limit)


@router.post("/api/chat")
async def api_chat(req: dict):
    """会话聊天：只发新消息，服务端拼上下文（自动截断）并记账。"""
    sid = str(req.get("sid") or "default")[:64]
    msg = str(req.get("message") or "").strip()
    task = req.get("task") or None
    if not msg:
        raise HTTPException(400, "message 为空")

    cfg = config.load()
    # 长期记忆检索注入（top-k 相关记忆作为 system prompt）
    mems = _memory.recall(msg)
    messages = _window(sid, cfg) + [{"role": "user", "content": msg}]
    if mems:
        sysprompt = "[长期记忆]\n" + "\n".join("- " + m["content"] for m in mems)
        messages = [{"role": "system", "content": sysprompt}] + messages
    payload = {"messages": messages, "temperature": 0.2}
    if req.get("max_tokens"):
        payload["max_tokens"] = int(req["max_tokens"])

    try:
        routes = _routing.choose(task=task, cfg=cfg)
    except _routing.BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except _routing.NoRoute as e:
        raise HTTPException(503, str(e))

    pol = cfg.get("policy") or {}
    tries = int(pol.get("max_retries") or 1) + 1
    last_err = ""
    for route in routes[:tries]:
        u, mid, key, kidx = route["upstream"], route["model_id"], route["key"], route["key_idx"]
        t0 = time.time()
        try:
            data = upstream.chat(u, mid, key, payload)
        except Exception as e:
            last_err = repr(e)[:200]
            if upstream.is_quota_exhausted_error(e):
                store.mark_free_exhausted(route["upstream_name"], mid,
                                          route["model"]["free"]["period"])
            store.mark_fail(u["name"], kidx, int(pol.get("cooldown_seconds") or 0))
            store.mark_model_fail(u["name"], mid, int(pol.get("cooldown_seconds") or 0))
            continue
        latency = int((time.time() - t0) * 1000)
        choice = (data.get("choices") or [{}])[0].get("message") or {}
        content = choice.get("content") or ""
        usage = data.get("usage") or {}
        pt = usage.get("prompt_tokens") or 0
        ct = usage.get("completion_tokens") or 0
        if not pt:
            pt, ct = pricing.estimate_tokens(messages, content)

        m = route["model"]
        is_free = bool(m["free"]["active"]) or m["unit"] <= 0
        cost = pricing.cost_of(m, pt, ct)
        store.record(route["upstream_name"], mid, kidx, "", pt, ct, cost, False, True,
                     latency, task or "", free=is_free,
                     price_in=m["price_in"], price_out=m["price_out"])
        if m["free"]["active"]:
            store.tick_model(route["upstream_name"], mid, m["free"]["period"], pt, cost)

        add(sid, "user", msg)
        add(sid, "assistant", content, model=mid, tokens=pt + ct)
        # 后台抽取长期记忆（不阻塞回复）
        _memory.extract_async(sid, messages[-6:] + [{"role": "assistant", "content": content}])
        return {"sid": sid, "content": content, "model": mid, "upstream": u["name"],
                "free": is_free, "reason": route.get("reason", ""),
                "tokens": {"prompt": pt, "completion": ct}, "cost_cny": round(cost, 6),
                "latency_ms": latency}
    raise HTTPException(502, "全部上游失败：" + last_err)


@router.get("/api/chat/history")
def api_history(sid: str = "default"):
    return {"sid": sid, "messages": history(sid, 500)}


@router.post("/api/chat/clear")
async def api_clear(req: dict):
    return {"sid": req.get("sid") or "default", "deleted": clear(str(req.get("sid") or "default")[:64])}


_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>llm-gateway 免费聊天</title><style>
body{font-family:system-ui,"Microsoft YaHei";max-width:860px;margin:0 auto;padding:12px;background:#0f1420;color:#dfe6f3}
h1{font-size:18px} .bar{color:#8fa3c8;font-size:12px;margin-bottom:8px}
#log{height:58vh;overflow-y:auto;border:1px solid #26314a;border-radius:8px;padding:10px;background:#131a2b}
.msg{margin:8px 0;padding:8px 10px;border-radius:8px;white-space:pre-wrap;word-break:break-word}
.user{background:#1d2c4f;margin-left:15%}.bot{background:#17223a;margin-right:15%}
.meta{font-size:11px;color:#7e93bb;margin-top:4px}
.free{color:#5dd39e}.paid{color:#e8a15d}
#row{display:flex;gap:8px;margin-top:10px}
#txt{flex:1;resize:none;height:64px;background:#131a2b;color:#dfe6f3;border:1px solid #26314a;border-radius:8px;padding:8px}
button,select{background:#22325c;color:#dfe6f3;border:1px solid #31456f;border-radius:8px;padding:8px 14px;cursor:pointer}
button:disabled{opacity:.5}
</style></head><body>
<h1>llm-gateway 免费聊天 <span id="qbar" class="bar"></span></h1>
<button onclick="show('chat')">聊天</button> <button onclick="show('mem')">记忆</button>
<div class="bar">服务端会话记忆（自动截断上下文）· 长期记忆自动抽取/检索注入 · 每条回复显示实际模型与 token 消耗</div>
<div id="view-chat">
<div id="log"></div>
<div id="row">
  <select id="task"><option value="">默认(balanced)</option><option value="cheap">cheap(小模型)</option>
  <option value="balanced">balanced</option><option value="quality">quality(旗舰免费)</option></select>
  <textarea id="txt" placeholder="输入消息，Ctrl+Enter 发送"></textarea>
  <button onclick="send()">发送</button><button onclick="cls()">清空</button>
</div>
</div>
<div id="view-mem" style="display:none">
  <div class="bar">长期记忆（自动从对话抽取 + 手动维护），跨会话生效。<span id="memcount"></span></div>
  <div id="memlog" style="height:52vh;overflow-y:auto;border:1px solid #26314a;border-radius:8px;padding:10px;background:#131a2b"></div>
  <div style="display:flex;gap:8px;margin-top:10px">
    <input id="memtxt" style="flex:1;background:#131a2b;color:#dfe6f3;border:1px solid #26314a;border-radius:8px;padding:8px" placeholder="手动添加一条记忆">
    <select id="memkind"><option value="fact">fact</option><option value="preference">preference</option></select>
    <button onclick="addMem()">添加</button><button onclick="loadMem()">刷新</button>
  </div>
</div>
<script>
const sid = localStorage.sid || (localStorage.sid = 's' + Date.now().toString(36));
const log = document.getElementById('log');
const txt = document.getElementById('txt');
const taskSel = document.getElementById('task');
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function push(role, html, meta){
  const d = document.createElement('div'); d.className = 'msg ' + role;
  d.innerHTML = html + (meta ? '<div class="meta">' + meta + '</div>' : '');
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
async function quota(){
  try{ const d = await (await fetch('/api/quota/debug')).json();
    const ms = Object.values(d.models||{});
    const free = ms.filter(m=>m.active).length;
    document.getElementById('qbar').textContent = '免费可用模型 ' + free + ' / ' + ms.length;
  }catch(e){}
}
async function restore(){
  try{ const d = await (await fetch('/api/chat/history?sid='+sid)).json();
    for(const m of d.messages){ if(m.role==='user') push('user', esc(m.content));
      else push('bot', esc(m.content), m.model ? esc(m.model) : ''); }
  }catch(e){}
}
async function send(){
  const msg = txt.value.trim(); if(!msg) return;
  txt.value=''; push('user', esc(msg));
  const w = push('bot', '<i>思考中…</i>', '');
  try{
    const r = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sid, message: msg, task: taskSel.value || null})});
    const d = await r.json();
    if(!r.ok){ w.innerHTML = '<span style="color:#e8756d">错误 ' + r.status + ': ' + esc(d.detail||'') + '</span>'; return; }
    w.innerHTML = esc(d.content) +
      '<div class="meta"><b class="' + (d.free?'free':'paid') + '">' + esc(d.upstream + '/' + d.model) +
      (d.free?' [免费]':' [付费]') + '</b> · prompt ' + d.tokens.prompt + ' + completion ' + d.tokens.completion +
      ' tok · ' + (d.latency_ms/1000).toFixed(1) + 's · ¥' + d.cost_cny + '</div>';
  }catch(e){ w.innerHTML = '<span style="color:#e8756d">请求失败: ' + esc(String(e)) + '</span>'; }
}
async function cls(){ await fetch('/api/chat/clear',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({sid})}); log.innerHTML=''; }
txt.addEventListener('keydown', e=>{ if(e.key==='Enter' && (e.ctrlKey||e.metaKey)) send(); });
function show(t){
  document.getElementById('view-chat').style.display = t==='chat'?'block':'none';
  document.getElementById('view-mem').style.display = t==='mem'?'block':'none';
  if(t==='mem') loadMem();
}
async function loadMem(){
  try{ const d = await (await fetch('/api/memory')).json();
    const box = document.getElementById('memlog'); box.innerHTML='';
    document.getElementById('memcount').textContent = '共 ' + d.memories.length + ' 条';
    if(!d.memories.length) box.innerHTML = '<div class="msg bot">还没有记忆。聊几句（说出你的偏好/背景），会自动抽取。</div>';
    for(const m of d.memories){
      const row = document.createElement('div'); row.className = 'msg bot';
      row.innerHTML = '<span class="free">[' + esc(m.kind) + ']</span> ' + esc(m.content) +
        '<div class="meta">命中 ' + m.hits + ' 次 · <a href="#" onclick="delMem(' + m.id + ');return false" style="color:#e8756d">删除</a></div>';
      box.appendChild(row);
    }
  }catch(e){}
}
async function addMem(){
  const v = document.getElementById('memtxt').value.trim(); if(!v) return;
  await fetch('/api/memory', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({content: v, kind: document.getElementById('memkind').value})});
  document.getElementById('memtxt').value=''; loadMem();
}
async function delMem(id){ await fetch('/api/memory/delete', {method:'POST',
  headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})}); loadMem(); }
quota(); restore();
</script></body></html>"""


@router.get("/ui", response_class=HTMLResponse)
def ui():
    return _PAGE
