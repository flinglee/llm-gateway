"""百炼控制台「免费额度」实时数据源（按模型剩余量/到期日）。

原理：控制台该页数据来自 zeldaEasy.bailian-commerce.freeTrial.queryFreeTierQuotaAsyn，
该接口受阿里云风控保护(需页面 JS 生成的 bx-ua 头)，无法用 AK/Cookie 直连。
故用 Playwright 复用已登录的持久化浏览器配置(.pw-profile)打开免费额度页，
拦截页面自身发出的 API 响应（风控头由页面自带），解析后写入缓存 JSON。
网关读缓存，不阻塞请求路径；缓存过期(默认10分钟)时后台线程静默刷新。

缓存结构：{"fetched_at": ms, "quotas": {model_id: {"remaining": int, "total": int,
"expire": "YYYY-MM-DD", "status": "VALID", "free_tier_only": bool}}}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, ".freetier_cache.json")
PROFILE = os.environ.get("LLMGW_PROFILE_DIR") or os.path.join(BASE, ".pw-profile")
PAGE_URL = "https://bailian.console.aliyun.com/cn-beijing/costing-balance/free-quota"
TTL_SECONDS = 600
_api = None          # {"quotas": {...}, "fetched_at": ms}
_lock = threading.Lock()


def _find_chrome() -> str:
    """探测 Playwright chromium 可执行文件；可用 LLMGW_CHROME_PATH 覆盖。"""
    custom = os.environ.get("LLMGW_CHROME_PATH")
    if custom and os.path.exists(custom):
        return custom
    import glob
    pats = [
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright",
                     "chromium-*", "chrome-win64", "chrome.exe"),      # Windows
        os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright",
                     "chromium-*", "chrome-linux", "chrome"),          # Linux
        os.path.join(os.path.expanduser("~/Library/Caches/ms-playwright"),
                     "chromium-*", "chrome-mac", "Chromium.app",
                     "Contents", "MacOS", "Chromium"),                 # macOS
    ]
    hits = []
    for p in pats:
        hits.extend(glob.glob(p))
    return sorted(hits)[-1] if hits else ""


def _expire_str(ms: float) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _parse(payload: dict) -> dict:
    """把 queryFreeTierQuotaAsyn 响应解析成 {model: {...}}。"""
    data = ((payload.get("data") or {}).get("DataV2") or {}).get("data") or {}
    data = data.get("data") or data
    out = {}
    for q in data.get("freeTierQuotas") or []:
        mid = str(q.get("model") or "")
        if not mid:
            continue
        out[mid] = {
            "remaining": int(q.get("quotaTotal") or 0),
            "total": int(q.get("quotaInitTotal") or 0),
            "expire": _expire_str(q.get("quotaValidityPeriod") or 0),
            "status": q.get("quotaStatus") or "",
            "free_tier_only": bool(q.get("freeTierOnly")),
        }
    return out


def fetch_once(timeout_ms: int = 45000) -> dict | None:
    """用持久化浏览器打开免费额度页，拦截全部 API 响应(按模型类型分批下发，需合并)。"""
    from playwright.sync_api import sync_playwright
    exe = _find_chrome()
    bodies = []
    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                PROFILE, executable_path=exe, headless=True,
                viewport={"width": 1400, "height": 900})
        except Exception:
            return None
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(resp):
            try:
                if "queryFreeTierQuota" in resp.url and resp.status == 200:
                    bodies.append(resp.json())
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            page.goto(PAGE_URL, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(10000)
        except Exception:
            pass
        if not bodies:
            try:
                bodies.append(page.evaluate(
                    "fetch('/data/api.json?action=BroadScopeAspnGateway&product=sfm_bailian"
                    "&api=zeldaEasy.bailian-commerce.freeTrial.queryFreeTierQuotaAsyn&_v=')"
                    ".then(r=>r.json())"))
            except Exception:
                pass
        ctx.close()
    if not bodies:
        return None
    quotas = {}
    for b in bodies:  # 合并各类型批次（后到覆盖同模型，数据一致）
        try:
            quotas.update(_parse(b))
        except Exception:
            pass
    if not quotas:
        return None
    cache = {"fetched_at": int(time.time() * 1000), "quotas": quotas}
    try:
        json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return cache


def _spawn_refresh():
    """后台子进程刷新（浏览器崩溃不影响网关进程）。"""
    def run():
        try:
            subprocess.run([sys.executable, os.path.abspath(__file__)],
                           timeout=120, capture_output=True)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def has_data() -> bool:
    """控制台免费额度清单是否已拿到过数据（含缓存文件）。

    free_left 用它做"完全权威"判定的前提：一旦有清单，
    不在清单里的模型一律视为按量付费（免费额度 0）。
    """
    global _api
    with _lock:
        if _api is None:
            try:
                _api = json.load(open(CACHE, encoding="utf-8"))
            except Exception:
                _api = None
        if _api is None:
            return False
        # 有缓存文件才算"有数据"；且要求 quotas 非空（抓取失败不会写文件，双保险）
        return bool((_api or {}).get("quotas"))


def get_model_quota(model_id: str, upstream_type: str | None = None) -> dict | None:
    """取某模型的实时免费额度信息；缓存过期则后台刷新。

    返回 {"remaining","total","expire","status",...}；无数据返回 None。
    """
    global _api
    with _lock:
        if _api is None:
            try:
                _api = json.load(open(CACHE, encoding="utf-8"))
            except Exception:
                _api = None
        fresh = _api and (time.time() * 1000 - _api.get("fetched_at", 0)) < TTL_SECONDS * 1000
        if not fresh and not _refreshing():
            spawn_refresh_with_alerts()
        q = (_api or {}).get("quotas", {}).get(model_id)
        return dict(q) if q else None


_refreshing_flag = [False]


def _refreshing() -> bool:
    return _refreshing_flag[0]


def _mark_refreshing():
    _refreshing_flag[0] = True


def login_once() -> bool:
    """弹出可见浏览器让用户重新登录控制台（会话票据过期时用），成功后立即刷新缓存。"""
    from playwright.sync_api import sync_playwright
    exe = _find_chrome()
    ok = False
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, executable_path=exe, headless=False,
            viewport={"width": 1500, "height": 950})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(PAGE_URL, timeout=40000, wait_until="domcontentloaded")
        except Exception:
            pass

        def ticket():
            try:
                return any(c.get("name") == "login_aliyunid_ticket"
                           for c in ctx.cookies(PAGE_URL))
            except Exception:
                return False

        # 最多等 10 分钟完成登录
        t0 = time.time()
        while time.time() - t0 < 600:
            if ticket():
                ok = True
                break
            time.sleep(3)
        if ok:
            time.sleep(5)
            ctx.close()
        else:
            ctx.close()
            return False
    fetch_once()
    return ok


# ---------------- 刷新调度 + 额度变动提醒 ----------------
def cache_snapshot() -> dict | None:
    """读当前缓存文件（供刷新前后对比，检测额度变动）。"""
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return None


def spawn_refresh_blocking(timeout: int = 240) -> bool:
    """阻塞式**子进程**刷新：浏览器/Playwright 全在子进程，主进程零阻塞。

    不捕获子进程输出——它的进度/摘要直接打进网关日志，方便排查。
    """
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def spawn_refresh_with_alerts() -> None:
    """惰性刷新（缓存过期且有流量时）：子进程刷新 + 完成后对比前后清单打额度提醒。

    子进程输出被捕获（避免刷屏）；提醒照常打印并持久化。
    """
    prev = cache_snapshot()
    _mark_refreshing()

    def run():
        try:
            subprocess.run([sys.executable, os.path.abspath(__file__)], timeout=240,
                           capture_output=True)
        except Exception:
            pass
        finally:
            _refreshing_flag[0] = False
        try:
            cur = cache_snapshot()
            if cur and (not prev or cur.get("fetched_at") != (prev or {}).get("fetched_at")):
                al = diff_alerts(prev, cur)
                if al:
                    save_alerts(al)
                    for a in al:
                        print("[额度提醒] " + a, flush=True)
        except Exception as e:
            print("[额度提醒] 对比异常:", repr(e)[:120], flush=True)

    threading.Thread(target=run, daemon=True, name="free-quota-lazy").start()


def diff_alerts(prev: dict | None, cur: dict) -> list[str]:
    """对比刷新前后清单，生成额度变动提醒（用完/过期/恢复/首次清单）。"""
    out = []
    pq = (prev or {}).get("quotas") or {}
    cq = (cur or {}).get("quotas") or {}
    for mid, v in cq.items():
        rem = int(v.get("remaining") or 0)
        st = (v.get("status") or "").upper()
        p = pq.get(mid) or {}
        prem = int(p.get("remaining") or 0)
        pst = (p.get("status") or "").upper()
        if prem > 0 and rem <= 0:
            out.append("模型 %s 免费额度已用完（剩 0）" % mid)
        elif pst == "VALID" and st in ("EXHAUSTED", "EXPIRED"):
            out.append("模型 %s 免费额度状态变为 %s" % (mid, st))
        elif prem <= 0 and rem > 0 and pst:
            out.append("模型 %s 免费额度恢复/新增：%d tokens" % (mid, rem))
    if not pq and cq:
        n = sum(1 for v in cq.values() if int(v.get("remaining") or 0) > 0)
        out.append("首次拿到额度清单：共 %d 个模型，其中 %d 个有免费额度" % (len(cq), n))
    return out


def save_alerts(alerts: list) -> None:
    """提醒并入缓存文件持久化（保留最近 50 条；同一条提醒每天最多记一次）。"""
    if not alerts:
        return
    cur = cache_snapshot() or {}
    old = cur.get("alerts") or []
    day = time.strftime("%m-%d")
    seen = {a.split("] ", 1)[-1] + "|" + a[1:7] for a in old if a.startswith("[")}
    stamp = time.strftime("%m-%d %H:%M")
    for a in alerts:
        if (a + "|" + day) not in seen:
            old.append("[%s] %s" % (stamp, a))
    cur["alerts"] = old[-50:]
    try:
        json.dump(cur, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def alerts(limit: int = 30) -> list:
    c = cache_snapshot() or {}
    return list((c.get("alerts") or [])[-limit:])


# ---------------- 免费模型自动登记（免手填 providers.json） ----------------
_NON_CHAT_HINTS = ("embedding", "rerank", "asr", "tts", "omni", "realtime", "audio",
                   "livetranslate", "video", "ocr", "wan", "vsvr", "vlm", "matrx")


def sync_models_to_config() -> dict:
    """把 freetier 清单里「有免费额度」的对话类模型自动登记进 providers.json（bailian）。

    - 只新增：配置里已有的模型一律不动（人工改的价格/档位/开关不覆盖）；
    - 价格填保守占位（入 2 / 出 8 每百万）：有免费额度时路由本就免费优先、价格不参与；
      额度耗尽后它按普通付费模型参与"最便宜"竞争，占位价不至于被误选成最低价；
    - 非对话类（embedding/rerank/asr/tts/omni/audio 等）自动跳过，防止路由到不能对话的模型。
    """
    from .config import ROOT
    cur = cache_snapshot() or {}
    path = ROOT / "config" / "providers.json"
    try:
        prov = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": "providers.json 读取失败: %s" % str(e)[:80]}
    added, skipped = [], 0
    touched = False
    for u in prov.get("upstreams", []):
        if u.get("name") != "bailian":
            continue
        models = u.setdefault("models", [])
        have = {m.get("id") for m in models}
        for mid, v in (cur.get("quotas") or {}).items():
            if v.get("status") != "VALID" or (v.get("remaining") or 0) <= 0:
                continue
            low = mid.lower()
            if any(h in low for h in _NON_CHAT_HINTS):
                skipped += 1
                continue
            if mid in have:
                continue
            models.append({
                "id": mid, "label": mid, "tier": 1,
                "price_in": 2.0, "price_out": 8.0,
                "free_quota_tokens": int(v.get("remaining") or 0),
                "free_period": "month",
                "auto": True,
                "note": "freetier 自动登记 %s（占位价，额度耗尽后可人工改真实价）"
                        % time.strftime("%m-%d"),
            })
            have.add(mid)
            added.append(mid)
        touched = True
        break
    if added:
        try:
            json.dump(prov, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception as e:
            return {"ok": False, "error": "providers.json 写入失败: %s" % str(e)[:80]}
    return {"ok": touched, "added": added, "skipped_nonchat": skipped}


if __name__ == "__main__":
    c = fetch_once()
    print("fetched:", "ok" if c else "FAIL", "models:", len((c or {}).get("quotas", {})))
