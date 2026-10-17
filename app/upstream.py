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
    with _request(url, body, key, u.get("timeout", 60)) as r:
        return json.loads(r.read().decode("utf-8"))


def chat_stream(u: dict, model_id: str, key: str | None, payload: dict):
    """SSE 流式转发生成器（逐字节产出）。"""
    url = base(u) + "/chat/completions"
    body = dict(payload)
    body["model"] = model_id
    body["stream"] = True
    resp = _request(url, body, key, u.get("timeout", 60), stream=True)
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
