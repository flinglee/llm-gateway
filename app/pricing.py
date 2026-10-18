"""价目表：档位(tier)、单价、免费额度，按"先免费、后最便宜"选模型。

- tier: 1 便宜 / 2 均衡 / 3 高质；price_in/out 单位 ¥ / 1M tokens。
- free_quota_tokens: 该模型的免费/限免额度（百炼活动赠送）；0 表示无免费额度。
- free_period: 额度计量周期 day|month|forever（默认 month，限免多为当月有效）。
"""
from __future__ import annotations

from . import store
from . import quota
from . import freetier
from .config import load

TIERS = {"cheap": 1, "balanced": 2, "quality": 3, "free": 1}
EMBEDDING_CAP = "embedding"
# 非文本生成能力：通用文本对话候选池应排除（否则最便宜的语音/向量模型会被误选为对话模型）
NON_TEXT_CAPS = ("embedding", "audio", "asr")


def min_tier(task: str) -> int:
    return TIERS.get((task or "").lower(), 2)


def free_left(m: dict, upstream: dict | None = None) -> int:
    """该模型剩余免费 tokens（未配置免费额度返回 0）。

    优先级：① 百炼控制台「免费额度」页实时数据（freetier，权威：每模型剩余/到期/状态）；
            ② live_quota(BSS 计划状态) / free_expire 配置；
            ③ 百炼报错确认已耗尽(reactive) → 直接 0；
            ④ 本地估算 = 配置额度 − 经本网关用量。
    """
    q = int(m.get("free_quota_tokens") or 0)
    if q <= 0:
        return 0
    # ① 控制台实时数据完全权威（一旦拿到过数据）：
    #    VALID=按剩余量；其余任何状态(UNKNOWN/EXPIRED/EXHAUSTED)或不在清单里 = 按量付费(0免费)
    #    —— qwen-max/397B 这类不在控制台免费清单的模型绝不能再被误标为免费
    ft = freetier.get_model_quota(m["id"])
    if ft is not None:
        st = str(ft.get("status") or "")
        if st == "VALID":
            exp = str(ft.get("expire") or "")
            if exp and exp != "1970-01-01":
                try:
                    from datetime import date
                    y, mo, d = (int(x) for x in exp.split("-"))
                    if date.today() > date(y, mo, d):
                        return 0
                except Exception:
                    pass
            return max(0, int(ft.get("remaining") or 0))
        return 0
    if freetier.has_data():
        return 0  # 清单里有数据但没有这个模型 = 无免费额度
    # ② 兜底：控制台数据从未拉到过(启动瞬间/抓取故障)才用 BSS/本地估算
    if _free_expired(m):
        return 0
    if upstream:
        rem = quota.live_remaining(upstream, m["id"])
        if rem is not None:
            return max(0, rem)
    period = m.get("free_period") or "month"
    if store.is_free_exhausted(m["upstream"], m["id"], period):
        return 0
    used = store.model_tokens(m["upstream"], m["id"], period)
    return max(0, q - used)


def _free_expired(m: dict) -> bool:
    """免费额度是否已过到期日（free_expire=YYYY-MM-DD，当天仍可用，次日 0 点失效）。"""
    exp = str(m.get("free_expire") or "").strip()
    if not exp:
        return False
    try:
        from datetime import date
        y, mo, d = (int(x) for x in exp.split("-"))
        return date.today() > date(y, mo, d)
    except Exception:
        return False


def _free_low_watermark() -> int:
    """免费额度低水位阈值（tokens）：剩余 <= 该值即视为"快耗尽"，路由切走、改走付费包。

    取自 policy.free_low_watermark；缺省 2000（相对 100万免费额度只是个小缓冲，避免把最后一点
    免费 token 烧光后触发百炼 403 / 误扣）。设为 0 表示"用尽最后一 token 才切"。
    """
    try:
        cfg = load()
        v = (cfg.get("policy") or {}).get("free_low_watermark")
        if v is None:
            return 2000
        return int(v)
    except Exception:
        return 2000


def free_info(m: dict, upstream: dict | None = None) -> dict:
    q = int(m.get("free_quota_tokens") or 0)
    period = m.get("free_period") or "month"
    left = free_left(m, upstream)
    threshold = _free_low_watermark()
    # 剩余 <= 低水位阈值时，即便还剩一点点免费额度也视为"已耗尽"，路由切到付费包（39元）。
    active = q > 0 and left > threshold
    return {"quota": q, "period": period, "left": left,
            "used": max(0, q - left) if q else 0,
            "active": active, "low": 0 < left <= threshold}


_FREE_LOW_WARNED: set[tuple[str, str]] = set()


def free_low_alert(upstream: dict, m: dict) -> str | None:
    """消费免费额度后调用：若剩余已降到低水位以下，返回一条告警文案（每模型每进程只报一次）。

    让运维"实施知道"哪个模型快耗尽——网关会主动切到付费，不再烧最后那点免费 token。
    """
    q = int(m.get("free_quota_tokens") or 0)
    if q <= 0:
        return None
    threshold = _free_low_watermark()
    if threshold <= 0:
        return None
    left = free_left(m, upstream)
    if 0 < left <= threshold:
        key = (m["upstream"], m["id"])
        if key in _FREE_LOW_WARNED:
            return None
        _FREE_LOW_WARNED.add(key)
        return ("免费额度低水位：%s/%s 剩余约 %s tokens（阈值 %s），网关已自动切到付费包，"
                "停止消耗免费额度。" % (m["upstream"], m["id"], f"{left:,}", f"{threshold:,}"))
    return None


def all_models(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load()
    out = []
    for u in cfg.get("upstreams", []):
        for m in u.get("models", []):
            q = {
                "upstream": u["name"],
                "upstream_label": u.get("label", u["name"]),
                "id": m["id"],
                "label": m.get("label", m["id"]),
                "tier": int(m.get("tier", 2)),
                "price_in": float(m.get("price_in", 0.0)),
                "price_out": float(m.get("price_out", 0.0)),
                "caps": m.get("caps", ""),
                "free_quota_tokens": int(m.get("free_quota_tokens") or 0),
                "free_period": m.get("free_period") or "month",
                "platform_free": bool(m.get("platform_free")),
                "enabled": bool(u.get("enabled")) and bool(u.get("api_keys")),
                "unit": float(m.get("price_in", 0)) + float(m.get("price_out", 0)),
            }
            q["free"] = free_info(q, u)
            out.append(q)
    return out


def _sort_key(m: dict):
    """免费额度未用尽的排最前（内部按单价）；之后按总价、档位。"""
    return (0 if m["free"]["active"] else 1, m["unit"], m["tier"], m["id"])


def _is_free(m: dict) -> bool:
    """该模型当前是否零成本可用。

    显式 platform_free=true 的模型（如硅基流动免费小模型、智谱 flash）视为平台免费，
    与控制台免费额度无关、永不计费；不再用 unit<=0 猜测（价目表漏填价格≠免费，
    qwen-flash 曾因价格占位 0 被误判 0 成本直连按量付费，烧光余额的根因之一）。
    """
    return bool(m["free"]["active"]) or bool(m.get("platform_free"))


def _allow_paid(cfg: dict | None = None) -> bool:
    """policy.allow_paid：是否允许路由到付费模型。false = 只用免费/0成本模型，
    付费模型一律不进候选池（硬闸，防止任何误配置再烧真金白银）。"""
    try:
        cfg = cfg or load()
        v = (cfg.get("policy") or {}).get("allow_paid")
        return True if v is None else bool(v)
    except Exception:
        return True


def candidates(min_tier_value: int = 1, cap: str | None = None, cfg: dict | None = None,
               include_free: bool = False) -> list[dict]:
    """候选模型：免费额度优先（同能力、任意档位均纳入），免费用尽后再按档位下限取最便宜付费。

    核心策略（按用户要求）：一个模型免费额度用尽 → 切到同能力(同类型)的另一个免费模型继续用；
    只有当同能力所有模型免费都耗尽，才落付费(39元包)。故免费模型不卡档位，付费模型卡档位下限。

    - cap 指定（如 embedding）→ 只用该能力模型，按价格排序、免费优先；
    - 对话场景（无 cap）一律排除向量/语音模型。
    """
    ms = [m for m in all_models(cfg) if m["enabled"]]
    if cap:
        ms = [m for m in ms if cap in (m["caps"] or "")]
        return sorted(ms, key=_sort_key)
    ms = [m for m in ms if not any(c in (m["caps"] or "") for c in NON_TEXT_CAPS)]
    floor = min_tier_value
    free_ms = [m for m in ms if _is_free(m) and m["tier"] >= floor]
    paid_ms = [] if not _allow_paid(cfg) else \
        [m for m in ms if not _is_free(m) and m["tier"] >= floor]
    if not free_ms and not paid_ms:
        # 兜底：该档位下限完全无候选(配置极端时) → 放松到全部，保证可用
        free_ms = [m for m in ms if _is_free(m)]
        if _allow_paid(cfg):
            paid_ms = [m for m in ms if not _is_free(m)]
        floor = 1
    # 排序：cheap 档免费组按单价(最便宜优先)；balanced/quality 档免费组先比能力(tier 高者优先)，
    # 同级内再按单价降序(价高≈更强，优选最强免费，反正不花钱)
    fkey = _sort_key if floor <= 1 else (lambda m: (-m["tier"], -m["unit"], m["id"]))
    return sorted(free_ms, key=fkey) + sorted(paid_ms, key=_sort_key)


def route_pool(task: str, cfg: dict | None = None, include_free: bool = False) -> list[dict]:
    """与 router.choose 完全一致的候选池（单一真相源，供推荐表/调试复用）。

    所有档位统一：免费额度跨档位优先（先薅完同能力所有免费模型），免费用尽后按档位下限取最便宜付费。
    """
    cfg = cfg or load()
    return candidates(min_tier(task), None, cfg)


def cheapest(task: str = "balanced", cap: str | None = None, cfg: dict | None = None,
             include_free: bool | None = None) -> dict | None:
    c = candidates(min_tier(task), cap, cfg, include_free)
    return c[0] if c else None


def find(model_id: str, cfg: dict | None = None) -> dict | None:
    for m in all_models(cfg):
        if m["id"] == model_id:
            return m
    return None


def _cache_ratio(cfg: dict | None = None) -> float:
    """上游对「缓存命中的输入 token」的计价折扣（相对 price_in）。

    policy.cache_input_ratio 配置；缺省 0.4（百炼 qwen 系/主流平台缓存输入约 2~5 折，
    取 0.4 让账本偏保守，接近真实扣费）。
    """
    try:
        cfg = cfg or load()
        v = (cfg.get("policy") or {}).get("cache_input_ratio")
        return float(v) if v is not None else 0.4
    except Exception:
        return 0.4


def cost_of(m: dict, prompt_tokens: int, completion_tokens: int,
            cached_prompt_tokens: int = 0, cfg: dict | None = None) -> float:
    """按价目算单次成本；cached_prompt_tokens 是命中上游 prompt 缓存的输入部分，
    按 _cache_ratio 折扣计价（未传时按 0 处理，行为与旧版一致）。"""
    ratio = _cache_ratio(cfg)
    cached = min(max(0, cached_prompt_tokens), max(0, prompt_tokens))
    fresh_in = max(0, prompt_tokens - cached)
    return round(m["price_in"] * fresh_in / 1_000_000
                 + m["price_in"] * ratio * cached / 1_000_000
                 + m["price_out"] * completion_tokens / 1_000_000, 6)


def estimate_tokens(messages: list[dict], completion: str = "") -> tuple[int, int]:
    """上游未返回 usage 时的粗估：中文按 1.5 字/token，其它按 4 字符/token。"""
    def tk(s: str) -> int:
        s = s or ""
        zh = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        rest = len(s) - zh
        return max(1, int(zh / 1.5 + rest / 4))
    pt = sum(tk(str(m.get("content") or "")) for m in (messages or []))
    ct = tk(completion) if completion else 0
    return pt, ct


def pool_for_category(cat: dict, cfg: dict | None = None) -> list[dict]:
    """按业务分类取候选池：能力(caps) + 档位下限 + 可选模型白名单，免费额度跨档位优先。

    注意：有 caps 要求的分类（vision/asr/embedding 等）必须直接从"满足能力的模型"里选，
    不能走对话候选池——那条池会主动排除向量模型，会把 embedding 分类打成空。

    免费模型（同能力任意档位）一律优先；付费模型坚持分类档位下限。
    """
    cfg = cfg or load()
    task = cat.get("task") or "balanced"
    caps = [c.strip() for c in (cat.get("caps") or "").split(",") if c.strip()]
    allow = set(cat.get("models") or [])
    floor = min_tier(task)

    if caps:
        pool = [m for m in all_models(cfg)
                if m["enabled"] and all(c in (m["caps"] or "") for c in caps)]
    else:
        pool = [m for m in all_models(cfg)
                if m["enabled"] and not any(c in (m["caps"] or "") for c in NON_TEXT_CAPS)]
    # 免费(含平台免费)与付费都卡档位下限；免费组在 balanced/quality 档先比能力再比单价
    free_ms = [m for m in pool if _is_free(m) and m["tier"] >= floor]
    paid_ms = [] if not _allow_paid(cfg) else \
        [m for m in pool if not _is_free(m) and m["tier"] >= floor]
    if not free_ms and not paid_ms:
        free_ms = [m for m in pool if _is_free(m)]
        if _allow_paid(cfg):
            paid_ms = [m for m in pool if not _is_free(m)]
        floor = 1
    fkey = _sort_key if floor <= 1 else (lambda m: (-m["tier"], -m["unit"], m["id"]))
    pool = sorted(free_ms, key=fkey) + sorted(paid_ms, key=_sort_key)

    if allow:
        pool = [m for m in pool if m["id"] in allow]
    return pool


def recommend_table(cfg: dict | None = None) -> dict:
    """每档推荐：与真实路由同源（route_pool），避免推荐与实际选择不一致。"""
    cfg = cfg or load()
    ms = [m for m in all_models(cfg) if m["enabled"] and EMBEDDING_CAP not in (m["caps"] or "")]
    top = max(ms, key=lambda m: m["unit"]) if ms else None
    out = {}
    for task in ("free", "cheap", "balanced", "quality"):
        pool = route_pool(task, cfg)
        if not pool:
            continue
        pick = pool[0]
        out[task] = {
            "pick": pick,
            "free_left": pick["free"]["left"],
            "saving_vs_top_pct": round((1 - pick["unit"] / top["unit"]) * 100, 1)
            if top and top["unit"] > 0 else 0.0,
        }
    return out
