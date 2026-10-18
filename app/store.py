"""SQLite 账本：用量明细 / 响应缓存 / Key 配额 / 模型免费额度。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import date, datetime

from .config import DATA_DIR

_DB = None
# 可重入：cache_clear/mark_fail 等会在持锁内再次调用 db()
_LOCK = threading.RLock()


def db() -> sqlite3.Connection:
    global _DB
    with _LOCK:
        if _DB is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _DB = sqlite3.connect(str(DATA_DIR / "gateway.db"), check_same_thread=False)
            _DB.row_factory = sqlite3.Row
            _init(_DB)
    return _DB


def _init(c: sqlite3.Connection):
    c.executescript(
        "CREATE TABLE IF NOT EXISTS usage ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, upstream TEXT, model TEXT,"
        " key_idx INTEGER, req_hash TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,"
        " cost REAL, cached INTEGER, ok INTEGER, latency_ms INTEGER, task TEXT, err TEXT,"
        " free INTEGER DEFAULT 0, price_in REAL DEFAULT 0, price_out REAL DEFAULT 0,"
        " category TEXT DEFAULT '', app TEXT DEFAULT '');"
        "CREATE TABLE IF NOT EXISTS cache ("
        " hash TEXT PRIMARY KEY, upstream TEXT, model TEXT, response TEXT, ts REAL);"
        "CREATE TABLE IF NOT EXISTS keystate ("
        " upstream TEXT, key_idx INTEGER, day TEXT, spend REAL, calls INTEGER, fails INTEGER,"
        " cooldown_until REAL, PRIMARY KEY(upstream,key_idx,day));"
        "CREATE TABLE IF NOT EXISTS modelstate ("
        " upstream TEXT, model TEXT, period TEXT, tokens INTEGER, calls INTEGER,"
        " spend REAL, PRIMARY KEY(upstream,model,period));"
        "CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);"
    )
    _migrate(c)
    c.commit()


def _migrate(c: sqlite3.Connection):
    """老库加列（免费额度记账、分类/应用维度都是后加的）。"""
    for table, col, decl in (("usage", "free", "INTEGER DEFAULT 0"),
                             ("usage", "price_in", "REAL DEFAULT 0"),
                             ("usage", "price_out", "REAL DEFAULT 0"),
                             ("usage", "category", "TEXT DEFAULT ''"),
                             ("usage", "app", "TEXT DEFAULT ''"),
                             ("usage", "cache_read_tokens", "INTEGER DEFAULT 0")):
        cols = [r[1] for r in c.execute("pragma table_info(%s)" % table)]
        if col not in cols:
            c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    # modelstate 加「免费额度已被百炼报错确认耗尽」标记：reactive 对齐，当期永久停用
    mcols = [r[1] for r in c.execute("pragma table_info(modelstate)")]
    if "free_exhausted" not in mcols:
        c.execute("ALTER TABLE modelstate ADD COLUMN free_exhausted INTEGER DEFAULT 0")


def today() -> str:
    return date.today().isoformat()


def period_key(period: str) -> str:
    now = datetime.now()
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "forever":
        return "forever"
    return now.strftime("%Y-%m")


# ---------- 用量 ----------
def record(upstream: str, model: str, key_idx: int | None, req_hash: str, pt: int, ct: int,
           cost: float, cached: bool, ok: bool, latency_ms: int, task: str, err: str = "",
           free: bool = False, price_in: float = 0.0, price_out: float = 0.0,
           category: str = "", app: str = "", cache_read: int = 0):
    c = db()
    with _LOCK:
        c.execute("INSERT INTO usage (ts,day,upstream,model,key_idx,req_hash,prompt_tokens,"
                  "completion_tokens,cost,cached,ok,latency_ms,task,err,free,price_in,price_out,"
                  "category,app,cache_read_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), today(), upstream, model, key_idx, req_hash, pt, ct,
                   cost, 1 if cached else 0, 1 if ok else 0, latency_ms, task, err,
                   1 if free else 0, price_in, price_out, category or "", app or "",
                   max(0, int(cache_read or 0))))
        if ok and not cached and key_idx is not None:
            # 预算只统计真实花费：免费额度内的调用(即使有标价)不计入日预算
            spend_cost = 0.0 if free else cost
            c.execute("INSERT INTO keystate (upstream,key_idx,day,spend,calls,fails,cooldown_until) "
                      "VALUES (?,?,?,?,1,0,0) ON CONFLICT(upstream,key_idx,day) DO UPDATE SET "
                      "spend=spend+excluded.spend, calls=calls+1",
                      (upstream, key_idx, today(), spend_cost))
        c.commit()


def tick_model(upstream: str, model: str, period: str, tokens: int, cost: float):
    """按模型累计 tokens（免费额度以此为计量口径）。"""
    c = db()
    with _LOCK:
        c.execute("INSERT INTO modelstate (upstream,model,period,tokens,calls,spend) "
                  "VALUES (?,?,?,?,1,?) ON CONFLICT(upstream,model,period) DO UPDATE SET "
                  "tokens=tokens+excluded.tokens, calls=calls+1, spend=spend+excluded.spend",
                  (upstream, model, period_key(period), tokens, cost))
        c.commit()


def model_tokens(upstream: str, model: str, period: str) -> int:
    r = db().execute("SELECT tokens FROM modelstate WHERE upstream=? AND model=? AND period=?",
                     (upstream, model, period_key(period))).fetchone()
    return int(r["tokens"]) if r else 0


def mark_free_exhausted(upstream: str, model: str, period: str):
    """百炼报错确认该模型免费额度已耗尽：当期永久标记为「免费组不可用」（reactive 对齐）。

    存进 modelstate 的 free_exhausted 列；period 用 period_key 计算，故跨周期(月/日)自动失效。
    """
    c = db()
    with _LOCK:
        c.execute("INSERT INTO modelstate (upstream,model,period,tokens,calls,spend,free_exhausted) "
                  "VALUES (?,?,?,0,0,0,1) ON CONFLICT(upstream,model,period) DO UPDATE SET "
                  "free_exhausted=1", (upstream, model, period_key(period)))
        c.commit()


def is_free_exhausted(upstream: str, model: str, period: str) -> bool:
    r = db().execute("SELECT free_exhausted FROM modelstate WHERE upstream=? AND model=? AND period=?",
                     (upstream, model, period_key(period))).fetchone()
    return bool(r and r["free_exhausted"])


def spend_today(upstream: str | None = None, category: str | None = None, app: str | None = None) -> float:
    """今日花费。默认全局；可按上游 / 业务分类 / 应用维度单独统计。"""
    c = db()
    if upstream:
        r = c.execute("SELECT SUM(spend) s FROM keystate WHERE day=? AND upstream=?", (today(), upstream)).fetchone()
        return float(r["s"] or 0.0)
    where, args = ["day=?"], [today()]
    if category:
        where.append("category=?")
        args.append(category)
    if app:
        where.append("app=?")
        args.append(app)
    r = c.execute("SELECT SUM(cost) s FROM usage WHERE " + " AND ".join(where), args).fetchone()
    return float(r["s"] or 0.0)


def key_spend(upstream: str, key_idx: int) -> float:
    r = db().execute("SELECT spend FROM keystate WHERE upstream=? AND key_idx=? AND day=?",
                     (upstream, key_idx, today())).fetchone()
    return float(r["spend"] if r else 0.0)


def mark_fail(upstream: str, key_idx: int, cooldown_seconds: int, cool: bool = False):
    """记录失败次数。默认只计数不冷却——单 Key 上游冷 Key 等于冷全部模型；
    模型级失败用 mark_model_fail 单独冷那个模型。cool=True 仅用于整 Key 级故障（预留）。"""
    c = db()
    with _LOCK:
        c.execute("INSERT INTO keystate (upstream,key_idx,day,spend,calls,fails,cooldown_until) "
                  "VALUES (?,?,?,0,0,1,0) ON CONFLICT(upstream,key_idx,day) DO UPDATE SET "
                  "fails=fails+1",
                  (upstream, key_idx, today()))
        if cool:
            c.execute("UPDATE keystate SET cooldown_until=? WHERE upstream=? AND key_idx=? AND day=?",
                      (time.time() + max(0, cooldown_seconds), upstream, key_idx, today()))
        c.commit()


def in_cooldown(upstream: str, key_idx: int) -> bool:
    r = db().execute("SELECT cooldown_until FROM keystate WHERE upstream=? AND key_idx=? AND day=?",
                     (upstream, key_idx, today())).fetchone()
    return bool(r and r["cooldown_until"] and r["cooldown_until"] > time.time())


# 模型级冷却（内存态）：单个模型失败只冷它自己，不再连坐整把 Key 的所有模型
_MODEL_COOL: dict[tuple, float] = {}


def mark_model_fail(upstream: str, model: str, cooldown_seconds: int):
    _MODEL_COOL[(upstream, model)] = time.time() + max(0, cooldown_seconds)


def model_in_cooldown(upstream: str, model: str) -> bool:
    t = _MODEL_COOL.get((upstream, model))
    return bool(t and t > time.time())


# ---------- 缓存 ----------
def hash_request(payload: dict, model: str) -> str:
    base = json.dumps({"m": model, "p": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def cache_get(h: str, ttl: int):
    r = db().execute("SELECT response,ts FROM cache WHERE hash=?", (h,)).fetchone()
    if not r:
        return None
    if ttl > 0 and (time.time() - r["ts"]) > ttl:
        cache_del(h)
        return None
    return json.loads(r["response"])


def cache_put(h: str, upstream: str, model: str, response: dict, max_entries: int):
    c = db()
    with _LOCK:
        c.execute("INSERT INTO cache (hash,upstream,model,response,ts) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(hash) DO UPDATE SET response=excluded.response, ts=excluded.ts",
                  (h, upstream, model, json.dumps(response, ensure_ascii=False), time.time()))
        n = c.execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
        if max_entries and n > max_entries:
            keep = int(max_entries * 0.8)
            c.execute("DELETE FROM cache WHERE hash IN (SELECT hash FROM cache ORDER BY ts ASC LIMIT ?)",
                      (n - keep,))
        c.commit()


def cache_del(h: str):
    with _LOCK:
        db().execute("DELETE FROM cache WHERE hash=?", (h,))
        db().commit()


def cache_clear() -> int:
    with _LOCK:
        n = db().execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
        db().execute("DELETE FROM cache")
        db().commit()
    return n


def cache_stats() -> dict:
    c = db()
    rows = c.execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
    size = c.execute("SELECT SUM(LENGTH(response)) s FROM cache").fetchone()["s"] or 0
    return {"entries": rows, "bytes": size}


# ---------- 统计 ----------
def stats(days: int = 7) -> dict:
    c = db()
    since = time.time() - days * 86400
    rows = [dict(r) for r in c.execute(
        "SELECT day, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, SUM(cost) cost,"
        " COUNT(*) calls, SUM(cached) hits, AVG(latency_ms) lat, SUM(free) free_calls,"
        " SUM(cache_read_tokens) cache_read FROM usage "
        "WHERE ts>=? GROUP BY day ORDER BY day", (since,)).fetchall()]
    by_model = [dict(r) for r in c.execute(
        "SELECT upstream, model, SUM(cost) cost, COUNT(*) calls, SUM(prompt_tokens) pt,"
        " SUM(completion_tokens) ct, SUM(free) free_calls, AVG(latency_ms) lat,"
        " SUM(cache_read_tokens) cache_read FROM usage WHERE ts>=? "
        "GROUP BY upstream, model ORDER BY cost DESC", (since,)).fetchall()]
    errors = [dict(r) for r in c.execute(
        "SELECT upstream, model, err, COUNT(*) n FROM usage WHERE ok=0 AND ts>=? "
        "GROUP BY upstream, model, err ORDER BY n DESC LIMIT 20", (since,)).fetchall()]
    by_category = [dict(r) for r in c.execute(
        "SELECT category, COUNT(*) calls, SUM(cost) cost, SUM(prompt_tokens+completion_tokens) tokens,"
        " SUM(cached) hits FROM usage WHERE ts>=? AND category<>'' GROUP BY category ORDER BY cost DESC",
        (since,)).fetchall()]
    by_app = [dict(r) for r in c.execute(
        "SELECT app, COUNT(*) calls, SUM(cost) cost, SUM(prompt_tokens+completion_tokens) tokens "
        "FROM usage WHERE ts>=? AND app<>'' GROUP BY app ORDER BY cost DESC", (since,)).fetchall()]
    # 走免费额度的请求若按原价应付多少（= 薅到的钱）
    saved = c.execute(
        "SELECT SUM((prompt_tokens*price_in + completion_tokens*price_out)/1000000.0) s,"
        " SUM(prompt_tokens+completion_tokens) t, COUNT(*) n FROM usage WHERE free=1 AND ts>=?",
        (since,)).fetchone()
    return {"daily": rows, "by_model": by_model, "errors": errors,
            "by_category": by_category, "by_app": by_app,
            "free_saving": {"saved_cny": round(saved["s"] or 0.0, 6),
                            "tokens": int(saved["t"] or 0), "calls": int(saved["n"] or 0)},
            "generated_at": datetime.now().isoformat(timespec="seconds")}
