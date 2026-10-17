"""业务场景分类：决定每个场景用什么档位、要什么能力、日预算上限。

- 客户端三种指定方式（优先级从高到低）：body 里的 `category` → 请求头 `x-task` →
  直接把分类名当 `model` 传（如 model="vision"）→ 都没给则用 default 分类。
- 分类只描述"要什么"，具体选哪个模型仍由 pricing/router 按免费额度与单价决定。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

FILE = ROOT / "config" / "categories.json"


def load() -> dict:
    cfg = json.loads(FILE.read_text(encoding="utf-8"))
    cfg.setdefault("default", "chat")
    cfg.setdefault("categories", [])
    cfg.setdefault("apps", [])
    cfg.setdefault("gateway_keys", [])
    return cfg


def names(cfg: dict | None = None) -> list[str]:
    return [c["name"] for c in (cfg or load()).get("categories", [])]


def get(name: str, cfg: dict | None = None) -> dict | None:
    for c in (cfg or load()).get("categories", []):
        if c["name"] == name:
            return c
    return None


def resolve(name: str | None, cfg: dict | None = None) -> dict:
    """取分类；不存在时回落到 default，再不行用内置 chat。"""
    cfg = cfg or load()
    cat = get(name, cfg) if name else None
    if cat:
        return cat
    d = get(cfg.get("default", "chat"), cfg)
    return d or {"name": "chat", "label": "通用对话", "task": "balanced", "caps": "",
                 "daily_budget_cny": 0.0}


def caps_of(cat: dict) -> list[str]:
    return [c.strip() for c in (cat.get("caps") or "").split(",") if c.strip()]


def allow_models(cat: dict) -> list[str]:
    return list(cat.get("models") or [])


def budget_of(cat: dict) -> float:
    return float(cat.get("daily_budget_cny") or 0)


def app_by_key(key: str, cfg: dict | None = None) -> str | None:
    """网关自有 Key → 应用名。"""
    import os
    cfg = cfg or load()
    k = (key or "").strip()
    if not k:
        return None
    for gk in cfg.get("gateway_keys", []):
        raw = gk.get("key") or ""
        if raw.startswith("${") and raw.endswith("}"):
            raw = os.environ.get(raw[2:-1], "") or ""
        if raw and raw == k:
            return gk.get("app")
    return None


def app_info(name: str, cfg: dict | None = None) -> dict | None:
    for a in (cfg or load()).get("apps", []):
        if a.get("name") == name:
            return a
    return None
