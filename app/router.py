"""路由：在预算/配额/冷却约束下，按"先免费额度、后最便宜"挑 (上游 × 模型 × Key)。"""
from __future__ import annotations

from . import categories, pricing, store
from .config import load


class BudgetExceeded(Exception):
    pass


class NoRoute(Exception):
    pass


def _upstream_by_name(cfg: dict, name: str) -> dict | None:
    for u in cfg.get("upstreams", []):
        if u["name"] == name:
            return u
    return None


def _active(u: dict) -> bool:
    """启用且：有 Key，或本地免 Key 服务。"""
    return bool(u.get("enabled")) and (bool(u.get("api_keys")) or u.get("no_auth"))


def _usable_keys(u: dict) -> list[tuple[int, str]]:
    keys = u.get("api_keys") or []
    out = []
    for i, k in enumerate(keys):
        if store.in_cooldown(u["name"], i):
            continue
        quota = float(u.get("daily_quota_cny") or 0)
        if quota > 0 and store.key_spend(u["name"], i) >= quota:
            continue
        out.append((i, k))
    return out


def _over_budget(cfg: dict, category: str | None = None, app: str | None = None,
                 ccfg: dict | None = None) -> bool:
    budget = float((cfg.get("policy") or {}).get("daily_budget_cny") or 0)
    if budget > 0 and store.spend_today() >= budget:
        return True
    if category:
        cat = categories.get(category, ccfg or cfg)
        cb = float((cat or {}).get("daily_budget_cny") or 0)
        if cb > 0 and store.spend_today(category=category) >= cb:
            return True
    if app:
        info = categories.app_info(app, ccfg or cfg) or {}
        ab = float(info.get("daily_budget_cny") or 0)
        if ab > 0 and store.spend_today(app=app) >= ab:
            return True
    return False


def _build_routes(model_list: list[dict], cfg: dict, reason: str) -> list[dict]:
    """构建路由；模型级冷却的排到后面——只有全部在冷却时才保留（重试好过 503）。"""
    routes, cooling = [], []
    for m in model_list:
        u = _upstream_by_name(cfg, m["upstream"])
        if not u or not _active(u):
            continue
        is_cooling = store.model_in_cooldown(u["name"], m["id"])
        for idx, key in _usable_keys(u):
            r = {"upstream": u, "upstream_name": u["name"], "model": m,
                 "model_id": m["id"], "key": key, "key_idx": idx,
                 "free_left": m["free"]["left"], "reason": reason}
            (cooling if is_cooling else routes).append(r)
    return routes + cooling


def _reason_for(m: dict, budget_downgrade: bool = False) -> str:
    if m["free"]["active"]:
        return "免费额度优先：还剩 {:,} tokens（{}）".format(m["free"]["left"], m["free"]["period"])
    if budget_downgrade:
        return "超预算→免费额度已尽，降级到最便宜付费档"
    return "无免费额度→已启用模型中最便宜者"


def choose(task: str = "balanced", model: str | None = None,
           cap: str | None = None, cfg: dict | None = None,
           category: str | None = None, app: str | None = None,
           ccfg: dict | None = None) -> list[dict]:
    """返回有序候选路由（失败时依次重试）。

    - 传了 category：按该业务场景（档位 + 能力 caps + 模型白名单）取池；
    - 否则按 task 档位取池。
    路由顺序（所有档位统一，免费跨档位优先）：
      同能力有免费额度的模型（内部按单价）→ 免费用尽后按档位下限取最便宜付费 → 超预算再降级。
      即：一个模型免费耗尽会自动切到同能力(同类型)另一个免费模型；同能力全免费耗尽才落付费(39元包)。
    """
    cfg = cfg or load()
    ccfg = ccfg or categories.load()
    pol = cfg.get("policy") or {}
    task = task or pol.get("default_task", "balanced")
    budget_downgrade = False
    cat_name = category

    if model:
        m = pricing.find(model, cfg)
        if not m:
            raise NoRoute("未知模型：%s" % model)
        if cap and cap not in (m["caps"] or ""):
            raise NoRoute("模型 %s 不支持 %s" % (model, cap))
        if _over_budget(cfg, cat_name, app, ccfg) and pol.get("budget_action", "downgrade") == "reject":
            raise BudgetExceeded("今日预算已用尽（分类 %s）" % (cat_name or "全局"))
        pool, reason = [m], "显式指定"
    else:
        if _over_budget(cfg, cat_name, app, ccfg):
            if pol.get("budget_action", "downgrade") == "reject":
                raise BudgetExceeded("今日预算已用尽（分类 %s / 应用 %s）" % (cat_name or "全局", app or "-"))
            budget_downgrade = True
            if cat_name:
                # 降级也要守住分类的能力要求（不能让 vision 降级成纯文本模型）
                pool = pricing.pool_for_category(categories.get(cat_name, ccfg) or
                                                 {"task": "cheap", "caps": cap or ""}, cfg)
            else:
                pool = pricing.candidates(1, cap, cfg)
            reason = _reason_for(pool[0], True) if pool else ""
        else:
            if cat_name:
                cat = categories.get(cat_name, ccfg) or {"task": task, "caps": cap or ""}
                pool = pricing.pool_for_category(cat, cfg)
                reason = "分类「%s」：" % cat_name + (_reason_for(pool[0], budget_downgrade) if pool else "")
            elif cap:
                # 指定能力（如 embedding）：只在该能力模型里选，免费额度跨档位优先
                pool = pricing.candidates(pricing.min_tier(task), cap, cfg)
                reason = _reason_for(pool[0], budget_downgrade) if pool else ""
            else:
                # 所有档位统一：免费额度跨档位优先（先薅完同能力所有免费模型），免费用尽后按档位下限取最便宜付费
                pool = pricing.route_pool(task, cfg)
                reason = _reason_for(pool[0], budget_downgrade) if pool else ""
        if not pool:
            raise NoRoute(_no_route_reason(cfg, cat_name))

    routes = _build_routes(pool, cfg, reason)
    if not routes:
        raise NoRoute(_no_route_reason(cfg))
    return routes


def _no_route_reason(cfg: dict, category: str | None = None) -> str:
    enabled = [u for u in cfg.get("upstreams", []) if u.get("enabled")]
    head = "分类 %s：无可用模型" % category if category else "无可用模型"
    if not enabled:
        return head + "（没有 enabled 的上游）"
    no_key = [u["name"] for u in enabled if not u.get("api_keys")]
    if len(no_key) == len(enabled):
        return head + "（已启用但缺少 Key：填 api_keys 或设置环境变量如 BAILIAN_API_KEY）"
    if category:
        cat = categories.get(category, ccfg or cfg) or {}
        caps = [c.strip() for c in (cat.get("caps") or "").split(",") if c.strip()]
        allow = cat.get("models") or []
        if caps or allow:
            return head + ("（要求 caps=%s models=%s 的模型未启用或无免费额度/Key，"
                           "请在 providers.json 增配对应模型）" % (caps or "-", allow or "-"))
    return head + "（所有可用 Key 已达日额度或在冷却中）"


def keys_left(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load()
    out = []
    for u in cfg.get("upstreams", []):
        if not u.get("enabled"):
            continue
        keys = u.get("api_keys") or []
        for i, _k in enumerate(keys):
            quota = float(u.get("daily_quota_cny") or 0)
            out.append({
                "upstream": u["name"], "key_idx": i,
                "spend": round(store.key_spend(u["name"], i), 6),
                "quota": quota,
                "left": round(max(0.0, quota - store.key_spend(u["name"], i)), 6) if quota else None,
                "cooldown": store.in_cooldown(u["name"], i),
            })
    return out
