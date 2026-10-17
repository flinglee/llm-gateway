"""配置的 Web 管理：上游 Key / 网关访问 Key / 模型价格与免费额度。

- 所有写操作都要带 `x-admin-key`（默认 gw-admin，可用环境变量 GATEWAY_ADMIN_KEY 改）；
- 改动直接写回 config/*.json；配置是每次请求热加载的，保存即生效、无需重启；
- 读接口一律脱敏（只回掩码）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException

from .config import ROOT
from . import categories

router = APIRouter(prefix="/api/config", tags=["config"])

PROVIDERS = ROOT / "config" / "providers.json"
CATEGORIES = ROOT / "config" / "categories.json"


def _admin_key() -> str:
    return os.environ.get("GATEWAY_ADMIN_KEY") or "gw-admin"


def _check(x_admin_key: str | None):
    if (x_admin_key or "") != _admin_key():
        raise HTTPException(403, "管理密钥不对：请求头 x-admin-key 应为 %s" % _admin_key())


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _mask(k: str) -> str:
    k = k or ""
    if not k:
        return "(空)"
    if k.startswith("${"):
        return k + "（环境变量占位）"
    return (k[:6] + "…" + k[-4:]) if len(k) >= 12 else "***"


@router.get("")
def get_config(x_admin_key: str | None = Header(default=None)):
    """脱敏的全量配置（控制台用）。同样要求管理密钥——否则等于把默认密钥提示出去。"""
    _check(x_admin_key)
    prov = _read(PROVIDERS)
    cats = _read(CATEGORIES)
    for u in prov.get("upstreams", []):
        u["api_keys_masked"] = [_mask(k) for k in (u.get("api_keys") or [])]
        u["api_keys"] = []
    cats["gateway_keys"] = [{"key": _mask(g.get("key")), "app": g.get("app"),
                             "note": g.get("note", "")} for g in cats.get("gateway_keys", [])]
    return {"providers": prov, "categories": cats}


@router.post("/upstreams/{name}/enabled")
def set_enabled(name: str, payload: dict = Body(...), x_admin_key: str | None = Header(default=None)):
    _check(x_admin_key)
    prov = _read(PROVIDERS)
    for u in prov.get("upstreams", []):
        if u.get("name") == name:
            u["enabled"] = bool(payload.get("enabled"))
            _write(PROVIDERS, prov)
            return {"ok": True, "name": name, "enabled": u["enabled"]}
    raise HTTPException(404, "没有这个上游：%s" % name)


@router.post("/upstreams/{name}/keys")
def add_key(name: str, payload: dict = Body(...), x_admin_key: str | None = Header(default=None)):
    """添加上游 Key（body: {"key": "sk-..."}）。"""
    _check(x_admin_key)
    key = (payload.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key 不能为空")
    prov = _read(PROVIDERS)
    for u in prov.get("upstreams", []):
        if u.get("name") == name:
            keys = list(u.get("api_keys") or [])
            if key in keys:
                raise HTTPException(400, "这个 Key 已存在")
            keys.append(key)
            u["api_keys"] = keys
            u["enabled"] = bool(payload.get("enabled", True)) if "enabled" in payload else u.get("enabled", False)
            if "enabled" in payload:
                u["enabled"] = bool(payload["enabled"])
            _write(PROVIDERS, prov)
            return {"ok": True, "name": name, "count": len(keys)}
    raise HTTPException(404, "没有这个上游：%s" % name)


@router.delete("/upstreams/{name}/keys/{idx}")
def del_key(name: str, idx: int, x_admin_key: str | None = Header(default=None)):
    _check(x_admin_key)
    prov = _read(PROVIDERS)
    for u in prov.get("upstreams", []):
        if u.get("name") == name:
            keys = list(u.get("api_keys") or [])
            if idx < 0 or idx >= len(keys):
                raise HTTPException(404, "没有这个序号的 Key")
            keys.pop(idx)
            u["api_keys"] = keys
            _write(PROVIDERS, prov)
            return {"ok": True, "name": name, "count": len(keys)}
    raise HTTPException(404, "没有这个上游：%s" % name)


@router.post("/models")
def upsert_model(payload: dict = Body(...), x_admin_key: str | None = Header(default=None)):
    """改模型价格/免费额度（body: {upstream, id, price_in?, price_out?, free_quota_tokens?, free_period?}）。"""
    _check(x_admin_key)
    up, mid = payload.get("upstream"), payload.get("id")
    if not up or not mid:
        raise HTTPException(400, "需要 upstream 和 id")
    prov = _read(PROVIDERS)
    for u in prov.get("upstreams", []):
        if u.get("name") == up:
            for m in u.get("models", []):
                if m.get("id") == mid:
                    for f in ("price_in", "price_out"):
                        if f in payload:
                            m[f] = float(payload[f])
                    if "free_quota_tokens" in payload:
                        m["free_quota_tokens"] = int(payload["free_quota_tokens"])
                    if "free_period" in payload:
                        m["free_period"] = payload["free_period"]
                    _write(PROVIDERS, prov)
                    return {"ok": True, "model": m}
    raise HTTPException(404, "没有这个模型：%s/%s" % (up, mid))


@router.post("/gateway-keys")
def add_gateway_key(payload: dict = Body(...), x_admin_key: str | None = Header(default=None)):
    """给某个应用发一把网关访问 Key（body: {"key": "...", "app": "..."}）。"""
    _check(x_admin_key)
    key = (payload.get("key") or "").strip()
    app = (payload.get("app") or "").strip()
    if not key or not app:
        raise HTTPException(400, "key 和 app 都不能为空")
    cats = _read(CATEGORIES)
    for g in cats.get("gateway_keys", []):
        if g.get("key") == key:
            raise HTTPException(400, "这把 Key 已存在")
    cats.setdefault("gateway_keys", []).append({"key": key, "app": app})
    if app not in [a.get("name") for a in cats.get("apps", [])]:
        cats.setdefault("apps", []).append({"key": key, "name": app, "label": app,
                                            "daily_budget_cny": 0.0,
                                            "note": "由控制台创建"})
    _write(CATEGORIES, cats)
    return {"ok": True, "app": app}


@router.delete("/gateway-keys/{idx}")
def del_gateway_key(idx: int, x_admin_key: str | None = Header(default=None)):
    _check(x_admin_key)
    cats = _read(CATEGORIES)
    keys = cats.get("gateway_keys", [])
    if idx < 0 or idx >= len(keys):
        raise HTTPException(404, "没有这个序号的网关 Key")
    removed = keys.pop(idx)
    _write(CATEGORIES, cats)
    return {"ok": True, "removed": {"app": removed.get("app")}}


@router.get("/admin-key")
def admin_key_hint():
    return {"admin_key": _admin_key(),
            "note": "写操作请求头带 x-admin-key；可用环境变量 GATEWAY_ADMIN_KEY 修改"}
