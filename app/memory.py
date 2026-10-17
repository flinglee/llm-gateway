# -*- coding: utf-8 -*-
"""长期记忆层（v0.4）。

- 抽取：每轮对话后用 cheap 免费模型从最近几条消息里提取值得长期记住的信息
  （用户偏好/身份/项目事实），后台线程执行，不阻塞回复；
- 去重：新记忆经免费向量模型嵌入后与库内做余弦相似度，>0.88 视为重复丢弃；
- 检索：每轮 /api/chat 把用户消息嵌入，取相似度最高的 top-k 记忆注入 system prompt；
- 自进化预留：hits 记录被检索次数，供后续记分板统计。

全部走免费模型（cheap 档对话 + 免费向量），成本为零。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException

from . import config, store, upstream
from . import router as _routing

router = APIRouter()

DB = config.DATA_DIR / "gateway.db"
MAX_MEMORIES = 500
DEDUP_SIM = 0.88
TOP_K = 3
MIN_SIM = 0.30


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT DEFAULT 'fact',
        content TEXT NOT NULL,
        embedding TEXT DEFAULT '',
        sid TEXT DEFAULT '',
        hits INTEGER DEFAULT 0,
        created_at REAL, updated_at REAL)""")
    return conn


# ---------- 向量 ----------

def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(text: str) -> list | None:
    """用免费向量模型嵌入；失败返回 None（调用方自行降级）。"""
    try:
        cfg = config.load()
        routes = _routing.choose(cap="embedding", cfg=cfg)
        for route in routes[:2]:
            try:
                data = upstream.embeddings(route["upstream"], route["model_id"],
                                           route["key"], [text[:1000]])
                vec = (data.get("data") or [{}])[0].get("embedding")
                if vec:
                    return [float(x) for x in vec]
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------- CRUD ----------

def _insert(kind: str, content: str, sid: str) -> dict:
    vec = _embed(content)
    emb = json.dumps(vec) if vec else ""
    now = time.time()
    with _db() as c:
        if vec:
            dup = c.execute("SELECT id, content, embedding FROM memories").fetchall()
            for _id, _c, _e in dup:
                try:
                    if _e and _cosine(vec, json.loads(_e)) > DEDUP_SIM:
                        c.execute("UPDATE memories SET updated_at=? WHERE id=?", (now, _id))
                        return {"id": _id, "content": _c, "duplicate": True}
                except Exception:
                    continue
        cur = c.execute(
            "INSERT INTO memories(kind, content, embedding, sid, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)", (kind, content, emb, sid, now, now))
        mid = cur.lastrowid
        n = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if n > MAX_MEMORIES:  # 淘汰最久未命中的
            c.execute("DELETE FROM memories WHERE id IN "
                      "(SELECT id FROM memories ORDER BY hits, updated_at LIMIT ?)",
                      (n - MAX_MEMORIES,))
    return {"id": mid, "content": content, "duplicate": False}


@router.get("/api/memory")
def list_memories():
    with _db() as c:
        rows = c.execute("SELECT id, kind, content, hits, updated_at FROM memories"
                         " ORDER BY updated_at DESC").fetchall()
    return {"memories": [{"id": r[0], "kind": r[1], "content": r[2],
                          "hits": r[3], "updated_at": r[4]} for r in rows]}


@router.post("/api/memory")
async def add_memory(req: dict):
    content = str(req.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content 为空")
    return _insert(str(req.get("kind") or "fact"), content[:500],
                   str(req.get("sid") or ""))


@router.post("/api/memory/delete")
async def delete_memory(req: dict):
    with _db() as c:
        n = c.execute("DELETE FROM memories WHERE id=?", (int(req.get("id") or 0),)).rowcount
    return {"deleted": n}


# ---------- 检索注入 ----------

def recall(query: str, k: int = TOP_K) -> list[dict]:
    """取与 query 最相关的 top-k 记忆；嵌入失败时退化为最近添加的 k 条。"""
    with _db() as c:
        rows = c.execute("SELECT id, content, embedding FROM memories").fetchall()
    if not rows:
        return []
    vec = _embed(query)
    picked = []
    if vec:
        scored = []
        for _id, content, emb in rows:
            try:
                sim = _cosine(vec, json.loads(emb)) if emb else 0.0
            except Exception:
                sim = 0.0
            scored.append((sim, _id, content))
        scored.sort(reverse=True)
        picked = [(i, c) for s, i, c in scored[:k] if s >= MIN_SIM]
        if picked:
            with _db() as c2:
                c2.executemany("UPDATE memories SET hits=hits+1 WHERE id=?",
                               [(i,) for i, _ in picked])
        return [{"id": i, "content": c} for i, c in picked]
    return [{"id": r[0], "content": r[1]} for r in rows[-k:]]  # 无向量兜底：取最近添加的


# ---------- 抽取 ----------

_EXTRACT_PROMPT = (
    "你是记忆抽取器。从对话中提取值得跨会话长期记住的信息：用户身份/偏好/项目背景/明确要求。"
    '只输出 JSON 数组，每项 {"kind":"fact 或 preference","content":"一句话"}；'
    "没有值得记的就输出 []。禁止解释。")

_EXTRACT_MIN = 10  # 双方内容都低于该长度则跳过抽取（避免"ok"式对话烧 token）


def extract_async(sid: str, messages: list[dict]):
    threading.Thread(target=_extract, args=(sid, messages), daemon=True).start()


def _extract(sid: str, messages: list[dict]):
    try:
        convo = "\n".join("%s: %s" % (m["role"], (m.get("content") or "")[:300])
                          for m in messages[-6:])
        if len(convo) < _EXTRACT_MIN * 2:
            return
        payload = {"messages": [{"role": "system", "content": _EXTRACT_PROMPT},
                                {"role": "user", "content": convo}],
                   "temperature": 0.1, "max_tokens": 300}
        cfg = config.load()
        routes = _routing.choose(task="cheap", cfg=cfg)
        for route in routes[:2]:
            try:
                data = upstream.chat(route["upstream"], route["model_id"],
                                     route["key"], payload)
            except Exception:
                continue
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            try:
                s, e = content.find("["), content.rfind("]")
                items = json.loads(content[s:e + 1]) if 0 <= s < e else []
            except Exception:
                items = []
            for it in items[:5]:
                if isinstance(it, dict) and str(it.get("content") or "").strip():
                    _insert(str(it.get("kind") or "fact"),
                            str(it["content"]).strip()[:500], sid)
            return
    except Exception:
        pass
