"""省钱网关入口：OpenAI 兼容 /v1 接口 + 用量/配额/缓存管理 API + 控制台。
零三方依赖实现（除 fastapi/uvicorn）：urllib 调上游、sqlite 记账。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import categories, config_admin, pricing, router, store, upstream, quota

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"

# 常见 OpenAI/Anthropic 模型名 → 省钱档位：老客户端改 base_url 即可自动省钱
ALIAS_TASK = {
    "auto": None, "gateway": None, "default": None,
    "gpt-3.5-turbo": "cheap", "gpt-4o-mini": "cheap",
    "gpt-4o": "quality", "gpt-4": "quality", "gpt-4-turbo": "quality",
    "claude-3-5-sonnet": "quality", "claude-3-haiku": "cheap",
}
AUTO_PREFIX = ("auto", "auto-cheap", "auto-balanced", "auto-quality")

app = FastAPI(title="llm-gateway", version="0.4.0",
              description="省钱版 OpenAI 兼容网关：按性价比选模型 / 缓存去重 / 多 Key 轮换 / 额度预算")
app.include_router(config_admin.router)

from . import ui as _ui  # noqa: E402  内置聊天页 + 会话记忆
app.include_router(_ui.router)

from . import memory as _memory  # noqa: E402  长期记忆层
app.include_router(_memory.router)


# ---------------- 后台定时任务：每日刷新百炼控制台免费额度 ----------------
def _free_quota_daemon():
    """每天定时用持久化浏览器刷新免费额度缓存（不阻塞请求；失败沿用旧缓存）。"""
    import time as _t
    from . import freetier
    interval = 24 * 3600  # 每天一次；有流量时 free_left 也会按 10 分钟 TTL 惰性刷新
    _t.sleep(20)          # 等服务先起来
    while True:
        try:
            c = freetier.fetch_once()
            if c:
                q = c.get("quotas") or {}
                valid = [k for k, v in q.items()
                         if v.get("status") == "VALID" and (v.get("remaining") or 0) > 0]
                print("[freetier] 每日刷新完成: 共%d个模型, 有免费额度%d个" % (len(q), len(valid)),
                      flush=True)
            else:
                print("[freetier] 刷新失败(登录态可能过期，本次沿用旧缓存)。"
                      "重新登录方法: 运行 python -c \"import app.freetier as f; f.login_once()\" "
                      "在弹出窗口完成登录即可", flush=True)
        except Exception as e:
            print("[freetier] 刷新异常:", repr(e)[:200], flush=True)
        _t.sleep(interval)


import threading as _threading
_threading.Thread(target=_free_quota_daemon, daemon=True,
                  name="free-quota-daily").start()


# ---------------- OpenAI 兼容接口 ----------------
@app.get("/v1/models")
def v1_models():
    cfg = router_compat_cfg()
    ccfg = router_compat_categories()
    ms = pricing.all_models(cfg)
    cats = [{"id": c["name"], "object": "model", "owned_by": "category",
             "label": c.get("label", c["name"])} for c in ccfg.get("categories", [])]
    return {"object": "list", "data": [{"id": "auto", "object": "model", "owned_by": "llm-gateway"}] +
            cats + [{"id": m["id"], "object": "model", "owned_by": m["upstream"]} for m in ms]}


def router_compat_cfg():
    from .config import load
    return load()


def router_compat_categories():
    from . import categories
    return categories.load()


def query_app(request: Request) -> str | None:
    """?app=xxx 也能带应用身份（不方便加头的场景）。"""
    return request.query_params.get("app")


@app.post("/v1/chat/completions")
async def v1_chat(request: Request, x_task: str | None = Header(default=None),
                  x_app_id: str | None = Header(default=None),
                  authorization: str | None = Header(default=None)):
    body = await request.json()
    raw_model = body.get("model") or "auto"
    stream = bool(body.get("stream"))
    cfg = router_compat_cfg()
    ccfg = router_compat_categories()
    pol = cfg.get("policy") or {}

    # 0) 应用身份：body.app > x-app-id 头 > Authorization 里的网关 Key 映射
    app = body.get("app") or x_app_id or query_app(request) \
        or categories.app_by_key((authorization or "").replace("Bearer ", ""), ccfg) or ""

    # 1) 决策：显式模型 / 业务分类 / 自动档位
    explicit, category = None, None
    low = raw_model.lower()
    if body.get("category"):
        category = body["category"]
        task = None
    elif low in categories.names(ccfg):
        category, task = low, None
    elif low.startswith("auto"):
        task = low.split("-", 1)[1] if "-" in low else None
        task = task or x_task or pol.get("default_task", "balanced")
    elif low in ALIAS_TASK:
        task = ALIAS_TASK[low] or x_task or pol.get("default_task", "balanced")
    elif pricing.find(raw_model, cfg):
        explicit = raw_model
        task = x_task or pol.get("default_task", "balanced")
    else:
        task = x_task or pol.get("default_task", "balanced")
    if category:
        cat = categories.get(category, ccfg)
        if not cat:
            raise HTTPException(400, "未知分类：%s（可用：%s）" % (category, ",".join(categories.names(ccfg))))
        task = None

    try:
        routes = router.choose(task=task, model=explicit, cfg=cfg,
                               category=category, app=app, ccfg=ccfg)
    except router.BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except router.NoRoute as e:
        raise HTTPException(503, str(e))

    payload = {k: v for k, v in body.items() if k not in ("model", "stream")}
    payload.setdefault("temperature", 0.2)

    # 2) 缓存（非流式 + 低温度）：命中直接返回，零花费
    req_hash = None
    use_cache = bool(pol.get("cache_enabled")) and not stream and float(payload.get("temperature") or 0) <= 0.3
    if use_cache:
        req_hash = store.hash_request(payload, routes[0]["model_id"])
        hit = store.cache_get(req_hash, int(pol.get("cache_ttl_seconds") or 0))
        if hit:
            ps = pricing.estimate_tokens(payload.get("messages") or [], "")
            store.record(routes[0]["upstream_name"], routes[0]["model_id"], None, req_hash,
                         ps[0], 0, 0.0, True, True, 0, task or "", category=category or "", app=app)
            hit["gateway"] = {"upstream": routes[0]["upstream_name"], "model": routes[0]["model_id"],
                              "task": task, "category": category or "", "app": app,
                              "reason": "命中缓存（本次零花费）", "cost_cny": 0.0,
                              "cached": True, "saved_vs_quality_pct": 100.0}
            return hit

    tries = int(pol.get("max_retries") or 1) + 1
    last_err = ""
    for route in routes[:tries]:
        u, mid, key, kidx = route["upstream"], route["model_id"], route["key"], route["key_idx"]
        t0 = time.time()
        if stream:
            return _stream(u, mid, key, payload, route, task, cfg, category=category or "", app=app)
        try:
            data = upstream.chat(u, mid, key, payload)
        except Exception as e:
            last_err = repr(e)[:300]
            if upstream.is_quota_exhausted_error(e):
                # 百炼确认该模型免费额度耗尽 → 当期永久剔除免费组（reactive 对齐，避免反复重试失败）
                store.mark_free_exhausted(route["upstream_name"], mid, route["model"]["free"]["period"])
            store.mark_fail(u["name"], kidx, int(pol.get("cooldown_seconds") or 0))
            continue
        latency = int((time.time() - t0) * 1000)
        usage = data.get("usage") or {}
        pt = usage.get("prompt_tokens") or 0
        ct = usage.get("completion_tokens") or 0
        if not pt:
            pt, ct = pricing.estimate_tokens(payload.get("messages") or [],
                                             (data.get("choices") or [{}])[0].get("message", {}).get("content", ""))
        return _ok(data, route, pt, ct, latency, task, req_hash, payload, cfg,
                   category=category or "", app=app)
    raise HTTPException(502, "全部上游失败：" + last_err)


def _stream(u: dict, model_id: str, key: str | None, payload: dict, route: dict, task: str, cfg: dict,
            category: str = "", app: str = ""):
    """流式转发（不缓存）；结束后用估算输入 tokens 记账。"""
    pt, _ = pricing.estimate_tokens(payload.get("messages") or [])
    t0 = time.time()
    gen = upstream.chat_stream(u, model_id, key, payload)
    ok = {"v": True}

    def body_iter():
        try:
            for chunk in gen:
                yield chunk
        except Exception as ex:
            ok["v"] = False
            ok["err"] = ex
            raise
        finally:
            latency = int((time.time() - t0) * 1000)
            if ok["v"]:
                m = route["model"]
                is_free = bool(m["free"]["active"])
                cost = pricing.cost_of(m, pt, 0)
                store.record(route["upstream_name"], model_id, route["key_idx"], "",
                             pt, 0, cost, False, True, latency, task or "",
                             free=is_free, price_in=m["price_in"], price_out=m["price_out"],
                             category=category, app=app)
                if is_free:
                    store.tick_model(route["upstream_name"], model_id, m["free"]["period"], pt, cost)
                    warn = pricing.free_low_alert(route["upstream"], m)
                    if warn:
                        print("[quota]", warn, flush=True)
            else:
                if upstream.is_quota_exhausted_error(ok.get("err")):
                    store.mark_free_exhausted(route["upstream_name"], model_id,
                                              route["model"]["free"]["period"])
                store.mark_fail(route["upstream_name"], route["key_idx"],
                                int((cfg.get("policy") or {}).get("cooldown_seconds") or 0))

    return StreamingResponse(body_iter(), media_type="text/event-stream")


def _ok(data: dict, route: dict, pt: int, ct: int, latency: int, task: str,
        req_hash: str | None, payload: dict, cfg: dict,
        category: str = "", app: str = "") -> dict:
    m = route["model"]
    cost = pricing.cost_of(m, pt, ct)
    is_free = bool(m["free"]["active"])
    store.record(route["upstream_name"], route["model_id"], route["key_idx"], req_hash or "",
                 pt, ct, cost, False, True, latency, task or "",
                 free=is_free, price_in=m["price_in"], price_out=m["price_out"],
                 category=category, app=app)
    if is_free:  # 计入该模型的免费额度消耗（缓存命中不计，因为没有真调模型）
        store.tick_model(route["upstream_name"], route["model_id"], m["free"]["period"], pt + ct, cost)
        warn = pricing.free_low_alert(route["upstream"], m)
        if warn:
            print("[quota]", warn, flush=True)
    if req_hash:
        pol = cfg.get("policy") or {}
        store.cache_put(req_hash, route["upstream_name"], route["model_id"], data,
                        int(pol.get("cache_max_entries") or 0))
    data["gateway"] = {
        "upstream": route["upstream_name"], "model": route["model_id"], "key_idx": route["key_idx"],
        "task": task, "category": category, "app": app, "reason": route["reason"],
        "cost_cny": cost, "cached": False, "latency_ms": latency,
        "prompt_tokens": pt, "completion_tokens": ct,
        "free_used": is_free, "free_left": route["free_left"],
        "saved_vs_quality_pct": _saved_pct(route, cfg),
    }
    return data


def _saved_pct(route: dict, cfg: dict) -> float:
    ms = [m for m in pricing.all_models(cfg) if m["enabled"] and "embedding" not in (m["caps"] or "")]
    if not ms:
        return 0.0
    top = max(ms, key=lambda m: m["unit"])
    if top["unit"] <= 0:
        return 0.0
    return round((1 - route["model"]["unit"] / top["unit"]) * 100, 1)


@app.post("/v1/embeddings")
async def v1_embeddings(request: Request, x_app_id: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    body = await request.json()
    cfg = router_compat_cfg()
    ccfg = router_compat_categories()
    app = body.get("app") or x_app_id or query_app(request) \
        or categories.app_by_key((authorization or "").replace("Bearer ", ""), ccfg) or ""
    want = body.get("model")
    texts = body.get("input")
    texts = [texts] if isinstance(texts, str) else (texts or [])
    try:
        if want and pricing.find(want, cfg):
            routes = router.choose(model=want, cfg=cfg, category="embedding", app=app, ccfg=ccfg)
        else:
            routes = router.choose(category="embedding", cfg=cfg, app=app, ccfg=ccfg)
    except (router.BudgetExceeded, router.NoRoute) as e:
        raise HTTPException(503, str(e))
    last_err = ""
    for route in routes[:3]:
        t0 = time.time()
        try:
            data = upstream.embeddings(route["upstream"], route["model_id"], route["key"], texts)
        except Exception as e:
            last_err = repr(e)[:200]
            if upstream.is_quota_exhausted_error(e):
                store.mark_free_exhausted(route["upstream_name"], route["model_id"],
                                          route["model"]["free"]["period"])
            continue
        usage = data.get("usage") or {}
        pt = usage.get("prompt_tokens") or sum(pricing.estimate_tokens([{"content": t}])[0] for t in texts)
        cost = pricing.cost_of(route["model"], pt, 0)
        is_free = bool(route["model"]["free"]["active"])
        store.record(route["upstream_name"], route["model_id"], route["key_idx"], "",
                     pt, 0, cost, False, True, int((time.time() - t0) * 1000), "embedding",
                     free=is_free, price_in=route["model"]["price_in"],
                     price_out=route["model"]["price_out"], category="embedding", app=app)
        if is_free:
            store.tick_model(route["upstream_name"], route["model_id"],
                             route["model"]["free"]["period"], pt, cost)
            warn = pricing.free_low_alert(route["upstream"], route["model"])
            if warn:
                print("[quota]", warn, flush=True)
        data["gateway"] = {"upstream": route["upstream_name"], "model": route["model_id"],
                           "cost_cny": cost, "prompt_tokens": pt, "category": "embedding", "app": app,
                           "free_used": is_free}
        return data
    raise HTTPException(502, "向量调用失败：" + last_err)


# ---------------- 管理与统计 ----------------
@app.get("/api/health")
def api_health():
    cfg = router_compat_cfg()
    active = [u["name"] for u in cfg.get("upstreams", []) if u.get("enabled") and u.get("api_keys")]
    return {"ok": True, "active_upstreams": active, "spend_today": round(store.spend_today(), 6),
            "budget": (cfg.get("policy") or {}).get("daily_budget_cny")}


@app.get("/api/price")
def api_price():
    cfg = router_compat_cfg()
    return {"models": pricing.all_models(cfg), "recommend": pricing.recommend_table(cfg),
            "spend_today": round(store.spend_today(), 6),
            "budget": (cfg.get("policy") or {}).get("daily_budget_cny")}


@app.get("/api/recommend")
def api_recommend(task: str = "balanced"):
    cfg = router_compat_cfg()
    m = pricing.cheapest(task, cfg=cfg)
    return {"task": task, "pick": m}


@app.get("/api/keys")
def api_keys():
    return {"keys": router.keys_left(), "spend_today": round(store.spend_today(), 6)}


@app.get("/api/categories")
def api_categories():
    """业务分类 + 每个分类的今日花费/预算/当前推荐模型。"""
    ccfg = router_compat_categories()
    cfg = router_compat_cfg()
    out = []
    for c in ccfg.get("categories", []):
        pool = pricing.pool_for_category(c, cfg)
        spend = store.spend_today(category=c["name"])
        budget = float(c.get("daily_budget_cny") or 0)
        out.append({
            "name": c["name"], "label": c.get("label", c["name"]), "task": c.get("task"),
            "caps": c.get("caps", ""), "models": c.get("models", []),
            "budget": budget, "spend": round(spend, 6),
            "left": round(max(0.0, budget - spend), 6) if budget else None,
            "pick": pool[0]["id"] if pool else None,
            "pick_free_left": pool[0]["free"]["left"] if pool else 0,
            "candidates": [m["id"] for m in pool[:5]],
            "note": c.get("note", ""),
        })
    return {"default": ccfg.get("default"), "categories": out}


@app.post("/api/categories/reload")
def api_categories_reload():
    return {"default": router_compat_categories().get("default"),
            "count": len(categories.names())}


@app.get("/api/apps")
def api_apps():
    """接入的应用：今日花费/调用数/剩余预算。"""
    ccfg = router_compat_categories()
    out = []
    for a in ccfg.get("apps", []):
        spend = store.spend_today(app=a["name"])
        budget = float(a.get("daily_budget_cny") or 0)
        out.append({"name": a["name"], "label": a.get("label", a["name"]),
                    "budget": budget, "spend": round(spend, 6),
                    "left": round(max(0.0, budget - spend), 6) if budget else None,
                    "note": a.get("note", "")})
    return {"apps": out, "gateway_keys": [
        {"app": g.get("app"), "configured": _key_configured(g.get("key"))}
        for g in ccfg.get("gateway_keys", [])]}


def _key_configured(raw: str | None) -> bool:
    raw = (raw or "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        raw = os.environ.get(raw[2:-1], "") or ""
    return bool(raw)


@app.get("/api/stats")
def api_stats(days: int = 7):
    s = store.stats(days)
    s["cache"] = store.cache_stats()
    s["spend_today"] = round(store.spend_today(), 6)
    cfg = router_compat_cfg()
    s["budget"] = (cfg.get("policy") or {}).get("daily_budget_cny")
    return s


@app.post("/api/cache/clear")
def api_cache_clear():
    return {"cleared": store.cache_clear()}


@app.post("/api/probe")
def api_probe():
    """探测每个上游首选模型的连通性与延迟。"""
    cfg = router_compat_cfg()
    out = []
    for u in cfg.get("upstreams", []):
        if not u.get("enabled"):
            continue
        ms = u.get("models") or []
        ms = [m for m in ms if "embedding" not in (m.get("caps") or "")] or ms
        if not ms:
            continue
        key = (u.get("api_keys") or [None])[0]
        out.append({"upstream": u["name"], "model": ms[0]["id"], **upstream.probe(u, ms[0]["id"], key)})
    return {"probes": out}


@app.post("/api/config/reload")
def api_reload():
    return {"policy": router_compat_cfg().get("policy")}


@app.get("/api/quota/debug")
def api_quota_debug():
    """诊断：用配置的阿里云 AK 实拉一次百炼计费实例列表，返回原始结构 + 各模型免费计划判定。

    仅本地运维用。raw 是计费 OpenAPI QueryAvailableInstances 的真实返回；free_plans 给出每个
    百炼模型对应的「公共推理计划」(sfm_inference_public 系列) 是否有效（active=计划正常且未过期）。
    models 给出每个模型基于网关本地记账的免费剩余量(left)/是否仍可作为免费(active)/是否逼近低水位(low)，
    即"实施知道哪些快耗尽"的看板。注意：阿里云接口不返回逐模型剩余 token 数，只给计划 Status/EndTime。
    """
    cfg = router_compat_cfg()
    u = next((x for x in cfg.get("upstreams", []) if x.get("name") == "bailian"), None)
    if not u:
        return {"error": "no bailian upstream"}
    lq = u.get("live_quota") or {}
    if (lq.get("type") or "") != "aliyun-bss":
        return {"error": "bailian.live_quota.type 不是 aliyun-bss", "live_quota": lq}
    raw = quota._bss_describe_resource_packages(lq)
    plans = {}
    models = {}
    watermark = pricing._free_low_watermark()
    if raw:
        for m in pricing.all_models(cfg):
            if m["upstream"] == "bailian":
                plans[m["id"]] = quota._aliyun_bss_plan_info(lq, m["id"])
                fi = m["free"]
                models[m["id"]] = {"quota": fi["quota"], "left": fi["left"],
                                   "active": fi["active"], "low": fi.get("low"),
                                   "used": fi["used"]}
    return {"raw": raw, "free_plans": plans, "free_low_watermark": watermark, "models": models}


@app.exception_handler(HTTPException)
async def _as_openai_err(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"message": exc.detail, "type": "gateway_error",
                                           "code": exc.status_code},
                                 "gateway": {"ok": False}})


# 静态控制台必须最后挂载，否则 "/" 会吞掉 /api/* 与 /v1/*
if DASHBOARD.exists():
    app.mount("/", StaticFiles(directory=str(DASHBOARD), html=True), name="dashboard")
