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
            _mark_refreshing()
            _spawn_refresh()
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


if __name__ == "__main__":
    c = fetch_once()
    print("fetched:", "ok" if c else "FAIL", "models:", len((c or {}).get("quotas", {})))
