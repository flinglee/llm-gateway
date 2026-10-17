"""免费额度实时来源（可选）：从外部接口拉真实剩余额度，覆盖本地估算。

两种来源（upstream.live_quota.type 选择）：
  - "url"        ：简单 GET 一个地址（支持 {model}/{upstream} 占位），用 json_path 取剩余量。
                   适用于阿里云计费 AK 资源包接口的自建代理、或任何能返回 JSON 的地址。
  - "aliyun-bss" ：直接用阿里云 AccessKey 签名调用计费 OpenAPI（默认 QueryAvailableInstances），
                   查百炼「公共推理计划」(sfm_inference_public 系列) 的 Status/EndTime，
                   据此判断免费额度是否仍有效（接口不返回逐模型剩余 token 数）。

百炼控制台「模型用量」页的剩余量/过期时间是控制台专有数据，公开的 DashScope `sk-` Key 拉不到；
但同主账号的阿里云 AccessKey（授予 BSS 只读，如 AliyunBSSReadOnlyAccess）可以调计费 OpenAPI 查到。

未配置 / AK 缺失 / 调用失败时均返回 None，调用方回落到「本地计数 + 百炼报错学到的已耗尽标记」(reactive)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid

_CACHE: dict[str, tuple[float, object]] = {}
_LOCK = threading.Lock()


def _expand(v: str) -> str:
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return os.environ.get(v[2:-1], "") or ""
    return v


def live_remaining(upstream: dict, model_id: str) -> int | None:
    """返回该模型真实剩余免费 tokens（拿不到返回 None）。"""
    cfg = upstream.get("live_quota") or {}
    typ = cfg.get("type") or ("url" if cfg.get("url") else "")
    if typ == "aliyun-bss":
        return _aliyun_bss_remaining(cfg, model_id)
    # 默认：简单 url 拉取
    url = (cfg.get("url") or "").replace("{model}", model_id).replace("{upstream}", upstream.get("name", ""))
    if not url:
        return None
    ttl = int(cfg.get("ttl_seconds", 300))
    now = time.time()
    with _LOCK:
        c = _CACHE.get(url)
        if c and now - c[0] < ttl:
            return _pick_json(c[1], cfg, model_id)
    try:
        req = urllib.request.Request(url, headers={str(k): str(v) for k, v in (cfg.get("headers") or {}).items()})
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 10))) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    with _LOCK:
        _CACHE[url] = (now, data)
    return _pick_json(data, cfg, model_id)


# ---------------- 简单 url 来源 ----------------
def _pick_json(data: dict, cfg: dict, model_id: str) -> int | None:
    path = (cfg.get("json_path") or "").strip().strip(".")
    node: object = data
    if path:
        for part in path.split("."):
            if part == "{model}":
                node = node.get(model_id, {}) if isinstance(node, dict) else None
            elif isinstance(node, dict):
                node = node.get(part)
            else:
                return None
    if node is None:
        return None
    try:
        return int(node)
    except Exception:
        return None


# ---------------- 阿里云计费 OpenAPI (RPC 签名 V1) ----------------
def _percent(s: str) -> str:
    """阿里云 percentEncode：编码后 + → %20、* → %2A、%7E → ~。"""
    s = str(s)
    out = urllib.parse.quote(s, safe="")
    return out.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def _aliyun_sign(params: dict, secret: str) -> str:
    canon = "&".join(_percent(k) + "=" + _percent(v) for k, v in sorted(params.items()))
    string_to_sign = "GET&" + _percent("/") + "&" + _percent(canon)
    key = (secret + "&").encode("utf-8")
    return base64.b64encode(
        hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


def _bss_describe_resource_packages(cfg: dict) -> dict | None:
    """签名调用计费 OpenAPI（默认 QueryAvailableInstances），返回解析后的计划实例列表（缓存）。"""
    ak = _expand(cfg.get("access_key_id") or "")
    sk = _expand(cfg.get("access_key_secret") or "")
    if not ak or not sk:
        return None
    action = cfg.get("action", "DescribeResourcePackages")
    version = cfg.get("version", "2017-12-14")
    endpoint = cfg.get("endpoint", "https://business.aliyun.com").rstrip("/")
    region = cfg.get("region_id", "cn-hangzhou")
    extra = dict(cfg.get("extra_params") or {})
    ttl = int(cfg.get("ttl_seconds", 300))
    cache_key = "bss:" + endpoint + ":" + action + ":" + ak
    now = time.time()
    with _LOCK:
        c = _CACHE.get(cache_key)
        if c and now - c[0] < ttl:
            return c[1]
    params = {
        "Action": action,
        "Format": "JSON",
        "Version": version,
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "RegionId": region,
    }
    params.update({k: str(v) for k, v in extra.items()})
    params["Signature"] = _aliyun_sign(params, sk)
    url = endpoint + "/?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 15))) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # 调用失败不致命：返回 None，让上层回落到本地估算
        print("[quota] aliyun-bss 调用失败：", repr(e)[:200], flush=True)
        return None
    with _LOCK:
        _CACHE[cache_key] = (now, data)
    return data


import datetime

# 百炼免费额度挂在「公共推理计划」(sfm_inference_public 系列) 上。计费 OpenAPI 不返回逐模型剩余
# token 数，只返回计划实例的 Status / EndTime。因此用「计划是否有效」作为免费是否可用的信号：
#   - Status=Normal 且 EndTime 未过期 -> 免费可用（返回哨兵值，精确剩余未知，靠 reactive 兜底修正）
#   - Status=Expired / 已过期      -> 免费已耗尽（返回 0，网关立即路由到下一个上游，避免误扣付费包）
# 各模型 -> 计划 ProductType 的映射（可在 providers.json 的 live_quota.model_plans 覆盖/补充）。
# 注意：百炼"公共推理计划"会随活动更名。实测 2026-10 起文本主计划 sfm_inference_public_cn 已过期，
# 当前有效的文本免费额度在 sfm_tokenplansolo_public_cn（月度）/sfm_tokenplanextrausage_public_cn（长期）；
# 全模态在 sfm_inferenceOmni_public_cn、语音在 sfm_inferenceQwenAudio_public_cn、向量在 sfm_inferenceVector_public_cn，
# 这些均 Normal/有效。故默认把 qwen* 文本/视觉模型挂到 tokenplansolo，omni/audio 单独区分。
_FREE_PLAN_MAP = {
    "qwen-turbo": "sfm_tokenplansolo_public_cn",
    "qwen-plus": "sfm_tokenplansolo_public_cn",
    "qwen-max": "sfm_tokenplansolo_public_cn",
    "qwen-long": "sfm_tokenplansolo_public_cn",
    "qwen-vl-plus": "sfm_tokenplansolo_public_cn",
    "qwen-vl-max": "sfm_tokenplansolo_public_cn",
    "qwen2.5-7b-instruct": "sfm_tokenplansolo_public_cn",
    "qwen2.5-32b-instruct": "sfm_tokenplansolo_public_cn",
    "qwen2.5-72b-instruct": "sfm_tokenplansolo_public_cn",
    "qwen2-math-72b-instruct": "sfm_tokenplansolo_public_cn",
    "qwen3-omni-flash": "sfm_inferenceOmni_public_cn",
    "qwen3-omni-flash-realtime": "sfm_inferenceOmni_public_cn",
    "qwen-audio-3.0-asr-flash": "sfm_inferenceQwenAudio_public_cn",
    "qwen3-asr-flash-realtime": "sfm_inferenceQwenAudio_public_cn",
    "qwen3-tts-flash": "sfm_inferenceQwenAudio_public_cn",
    "sensevoice-v1": "sfm_inferenceQwenAudio_public_cn",
    "text-embedding-v3": "sfm_inferenceVector_public_cn",
    "text-embedding-v2": "sfm_inferenceVector_public_cn",
}


def _plan_for_model(model_id: str, cfg: dict) -> str | None:
    override = cfg.get("model_plans") or {}
    if model_id in override:
        return override[model_id]
    if model_id in _FREE_PLAN_MAP:
        return _FREE_PLAN_MAP[model_id]
    low = str(model_id).lower()
    if low.startswith("qwen"):
        # 按能力细分到仍有效的计划类型（避免全挂到已过期的 sfm_inference_public_cn）
        if "omni" in low:
            return "sfm_inferenceOmni_public_cn"
        if "audio" in low or "asr" in low or "tts" in low:
            return "sfm_inferenceQwenAudio_public_cn"
        return "sfm_tokenplansolo_public_cn"
    return None


def _plan_active(inst: dict) -> bool:
    if not isinstance(inst, dict) or inst.get("Status") != "Normal":
        return False
    end = inst.get("EndTime")
    if end:
        try:
            dt = datetime.datetime.strptime(str(end)[:19], "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            if dt < datetime.datetime.now(datetime.timezone.utc):
                return False
        except Exception:
            pass
    return True


def _bss_instances(cfg: dict) -> list:
    data = _bss_describe_resource_packages(cfg)
    if not data:
        return []
    insts = (data.get("Data") or {}).get("InstanceList") or []
    return insts if isinstance(insts, list) else []


def _aliyun_bss_plan_info(cfg: dict, model_id: str) -> dict | None:
    """该模型对应免费计划的诊断信息；无对应计划返回 None。"""
    ptype = _plan_for_model(model_id, cfg)
    if not ptype:
        return None
    for it in _bss_instances(cfg):
        if isinstance(it, dict) and it.get("ProductType") == ptype:
            active = _plan_active(it)
            return {
                "plan": ptype,
                "instance_id": it.get("InstanceID"),
                "status": it.get("Status"),
                "end_time": it.get("EndTime"),
                "active": active,
                # 计划过期/异常 -> 免费已耗尽(0)；计划有效 -> 不在此覆盖，回落网关本地记账
                "remaining": 0 if active is False else None,
            }
    return {"plan": ptype, "instance_id": None, "status": None,
            "end_time": None, "active": None, "remaining": None}


def _aliyun_bss_remaining(cfg: dict, model_id: str) -> int | None:
    """返回该模型真实剩余免费 tokens（拿不到返回 None 回落本地估算）。

    阿里云计费 OpenAPI（QueryAvailableInstances）只给『公共推理计划』(sfm_inference_public 系列)
    的 Status/EndTime，不给逐模型剩余 token 数。因此这里只做硬门禁：
      - 计划过期/异常  -> 返回 0（免费已耗尽，路由立即切到付费包，避免误扣）；
      - 计划有效       -> 返回 None，让 pricing.free_left 用网关本地记账
                         (基础额度 − 经本网关已用) 给出逐模型精确剩余。
    这样既拿到"免费是否还有效"的权威信号，又保留逐模型粒度的精确剩余。
    """
    info = _aliyun_bss_plan_info(cfg, model_id)
    if not info:
        return None
    if info["active"] is False:
        return 0
    return None
