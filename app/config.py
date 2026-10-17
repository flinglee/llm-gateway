"""配置加载：config/providers.json + .env + 环境变量占位 ${VAR}。

密钥一律放 .env（已 gitignore），providers.json 里只写 ${VAR} 占位，
这样配置文件可以直接分发而不泄露密钥。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "providers.json"
DATA_DIR = ROOT / "data"
ENV_PATH = ROOT / ".env"

DEFAULT_POLICY = {
    "default_task": "balanced",
    "daily_budget_cny": 5.0,
    "budget_action": "downgrade",
    "cache_enabled": True,
    "cache_ttl_seconds": 172800,
    "cache_max_entries": 20000,
    "cooldown_seconds": 180,
    "max_retries": 2,
}


def _load_dotenv() -> None:
    """轻量 .env 加载（KEY=VALUE，# 注释，不覆盖已有环境变量）。"""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()


def _expand(v: str) -> str:
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return os.environ.get(v[2:-1], "") or ""
    return v


def _deep_expand(obj):
    """递归展开配置里所有 ${VAR} 占位（api_keys / live_quota 里的 AK/SK 等都能用）。"""
    if isinstance(obj, dict):
        return {k: _deep_expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_expand(x) for x in obj]
    if isinstance(obj, str):
        return _expand(obj)
    return obj


def load() -> dict:
    cfg = _deep_expand(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    pol = dict(DEFAULT_POLICY)
    pol.update(cfg.get("policy") or {})
    cfg["policy"] = pol
    ups = []
    for u in cfg.get("upstreams", []):
        u = dict(u)
        keys = []
        raw = u.get("api_keys")
        if raw:
            keys = [k for k in (_expand(x) for x in raw) if k]
        elif u.get("api_key"):
            k = _expand(u["api_key"])
            keys = [k] if k else []
        u["api_keys"] = keys
        u.setdefault("timeout", 60)
        u.setdefault("enabled", False)
        u.setdefault("daily_quota_cny", 0.0)
        ups.append(u)
    cfg["upstreams"] = ups
    return cfg


def policy(cfg: dict | None = None) -> dict:
    return (cfg or load()).get("policy") or DEFAULT_POLICY


def upstreams(cfg: dict | None = None) -> list[dict]:
    return (cfg or load()).get("upstreams") or []
