"""上游调用：OpenAI 兼容 /chat/completions、/embeddings、/models（urllib，零三方依赖）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class UpstreamError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__("HTTP %s: %s" % (status, body[:300]))
        self.status = status
        self.body = body


# 百炼等上游在「免费额度耗尽 / 账户余额不足」时的报错特征（用于 reactive 对齐：学到后永久剔除免费组）
QUOTA_HINTS = ("额度", "quota", "免费", "用尽", "用完", "insufficient",
               "exhaust", "balance", "余额", "no quota", "free tier")


def is_quota_exhausted_error(e: BaseException) -> bool:
    """判断上游报错是否表示「免费额度已耗尽 / 余额不足」（而非普通超时、限流）。"""
    if not isinstance(e, UpstreamError):
        return False
    msg = ((e.body or "") + " " + str(e.status)).lower()
    return any(h in msg for h in QUOTA_HINTS)


def is_client_error(e: BaseException) -> bool:
    """4xx = 请求本身的问题（参数不兼容等），重试/冷却都无意义，不该把模型拉黑。"""
    return isinstance(e, UpstreamError) and 400 <= (e.status or 0) < 500


def classify(e: BaseException) -> str:
    """把上游异常归一成 failover 决策类别（统一错误分类层，对应 free-claude-code-main 的
    failure_policy 设计）：

    - quota_exhausted : 免费额度/余额耗尽 → 当期剔除免费组（持久），不再反复重试失败
    - auth            : 401/403 鉴权失败（Key 失效）→ 冷却该 Key 并告警，避免反复打失效 Key
    - client          : 其它 4xx 请求不兼容 → 不冷却，换下一条路由即可
    - rate_limited    : 429 限流 → 短冷却
    - overloaded      : 503/502/服务器忙 → 短冷却
    - network         : 连接/超时/读超时等瞬时故障 → 短冷却
    - unknown         : 其它 5xx/无状态码 → 当作瞬时故障短冷却

    下游据此决定「是否冷却、冷却谁、冷却多久」，并让路由自然对冷却模型做单探测恢复
    （冷却到期后的第一个请求即唯一探测，不再全量重试风暴）。
    """
    if not isinstance(e, UpstreamError):
        return "network"
    st = e.status or 0
    msg = ((e.body or "") + " " + str(e.status)).lower()
    if any(h in msg for h in QUOTA_HINTS):
        return "quota_exhausted"
    if st == 429:
        return "rate_limited"
    if 400 <= st < 500:
        return "auth" if st in (401, 403) else "client"
    if st in (502, 503) or "overload" in msg or "busy" in msg or "服务器忙" in msg:
        return "overloaded"
    return "network"


def _is_temperature_reject(e: BaseException) -> bool:
    """上游拒绝 temperature 参数（如 kimi-k3 只支持默认采样参数）。"""
    if not isinstance(e, UpstreamError) or e.status != 400:
        return False
    low = (e.body or "").lower()
    return "temperature" in low and ("not supported" in low or "invalid" in low)


def _headers(key: str | None) -> dict:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def _request(url: str, payload: dict, key: str | None, timeout: float, stream: bool = False):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in _headers(key).items():
        req.add_header(k, v)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise UpstreamError(e.code, body or str(e))
    except Exception as e:
        raise UpstreamError(0, repr(e))


def base(u: dict) -> str:
    return (u.get("base_url") or "").rstrip("/")


def chat(u: dict, model_id: str, key: str | None, payload: dict) -> dict:
    url = base(u) + "/chat/completions"
    body = dict(payload)
    body["model"] = model_id
    try:
        with _request(url, body, key, u.get("timeout", 60)) as r:
            return json.loads(r.read().decode("utf-8"))
    except UpstreamError as e:
        if _is_temperature_reject(e) and "temperature" in body:
            # 个别模型（kimi-k3 等）不支持自定义 temperature → 去掉采样参数重试一次
            body = {k: v for k, v in body.items()
                    if k not in ("temperature", "top_p", "presence_penalty",
                                 "frequency_penalty")}
            with _request(url, body, key, u.get("timeout", 60)) as r:
                return json.loads(r.read().decode("utf-8"))
        raise


def chat_stream(u: dict, model_id: str, key: str | None, payload: dict):
    """SSE 流式转发生成器（逐字节产出）；temperature 被拒时自动去参重试。"""
    url = base(u) + "/chat/completions"
    body = dict(payload)
    body["model"] = model_id
    body["stream"] = True
    try:
        resp = _request(url, body, key, u.get("timeout", 60), stream=True)
    except UpstreamError as e:
        if _is_temperature_reject(e) and "temperature" in body:
            body = {k: v for k, v in body.items()
                    if k not in ("temperature", "top_p", "presence_penalty",
                                 "frequency_penalty")}
            resp = _request(url, body, key, u.get("timeout", 60), stream=True)
        else:
            raise
    try:
        for chunk in resp:
            if chunk:
                yield chunk
    finally:
        try:
            resp.close()
        except Exception:
            pass


def embeddings(u: dict, model_id: str, key: str | None, input_texts: list[str]) -> dict:
    url = base(u) + "/embeddings"
    with _request(url, {"model": model_id, "input": input_texts}, key, u.get("timeout", 60)) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(u: dict, model_id: str, key: str | None) -> dict:
    """连通性/延迟探测：一次极小请求。"""
    import time
    t0 = time.time()
    try:
        chat(u, model_id, key, {"model": model_id,
                                "messages": [{"role": "user", "content": "ping"}],
                                "max_tokens": 1, "temperature": 0})
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000)}
    except UpstreamError as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "status": e.status, "error": e.body[:200]}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": repr(e)[:200]}
