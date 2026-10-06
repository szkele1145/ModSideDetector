"""网络层：只用标准库 ``urllib``（避免 ``requests`` 增加打包体积与依赖问题）。

统一处理：超时、重试（含 429/5xx 退避）、代理、UA、JSON 编解码。
所有失败都以 :class:`NetworkError` 抛出，调用方负责降级 —— **网络问题绝不能让
扫描整体失败**。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["NetworkError", "HttpResult", "HttpClient", "RateLimiter"]


class NetworkError(RuntimeError):
    """一次网络请求最终失败（已用尽重试）。"""


@dataclass
class HttpResult:
    """一次成功的 HTTP 响应。"""

    status: int
    body: bytes
    url: str

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NetworkError(f"响应不是合法 JSON：{exc!r}") from exc


class RateLimiter:
    """简单串行限流器：保证两次调用之间至少间隔 ``min_interval`` 秒。

    mcmod 实测 **0.6 秒/请求**安全，且**不要并发** —— 尊重对方服务器是硬要求。
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.monotonic() - self._last
        remaining = self.min_interval - delta
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()

    def mark(self) -> None:
        """记下「刚刚发生过一次请求」（用于外部已经发过请求的情况）。"""
        self._last = time.monotonic()


@dataclass
class HttpClient:
    """极简 HTTP 客户端（urllib 封装），线程安全（无共享可变状态）。"""

    user_agent: str = "ModSideDetector/1.0"
    timeout: float = 20.0
    max_retries: int = 3
    proxy: str = ""
    #: 重试基础退避秒数（第 n 次失败后等待 ``backoff * 2**(n-1)``）
    backoff: float = 0.8
    #: 每次成功请求后回调（用于日志/进度），签名 ``(url, status, elapsed)``
    on_request: Optional[Any] = None
    _opener: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        handlers: List[Any] = []
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------ 底层
    def request(
        self,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        method: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> HttpResult:
        """发一个请求并返回 :class:`HttpResult`；失败抛 :class:`NetworkError`。

        会重试的情况：连接错误、超时、HTTP 429、HTTP 5xx。
        4xx（除 429）视为确定性失败，立即抛出，避免无谓重试。
        """
        attempts = int(self.max_retries if retries is None else retries)
        attempts = max(1, attempts)
        merged = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if headers:
            merged.update({str(k): str(v) for k, v in headers.items()})
        last_error: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, headers=merged, method=method)
            started = time.monotonic()
            try:
                with self._opener.open(request, timeout=timeout or self.timeout) as response:
                    body = response.read()
                    result = HttpResult(status=int(response.status), body=body, url=url)
                    self._notify(url, result.status, time.monotonic() - started)
                    return result
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                try:
                    body = exc.read()
                except Exception:  # noqa: BLE001 - 读错误响应体失败不该掩盖原错误
                    body = b""
                last_error = exc
                if status == 429 or 500 <= status < 600:
                    retry_after = _retry_after_seconds(exc)
                    if attempt < attempts:
                        self._sleep(retry_after if retry_after is not None else self.backoff * (2 ** (attempt - 1)))
                        continue
                    raise NetworkError(f"HTTP {status}（已重试 {attempts} 次）：{url}") from exc
                raise NetworkError(f"HTTP {status}：{url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt < attempts:
                    self._sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                raise NetworkError(f"请求失败（已重试 {attempts} 次）：{exc!r}") from exc

        raise NetworkError(f"请求失败：{last_error!r}")

    def _sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, min(float(seconds or 0.0), 30.0)))

    def _notify(self, url: str, status: int, elapsed: float) -> None:
        if self.on_request is None:
            return
        try:
            self.on_request(url, status, elapsed)
        except Exception:  # noqa: BLE001 - 回调出错不影响主流程
            pass

    # ------------------------------------------------------------ 便捷方法
    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> HttpResult:
        return self.request(url, headers=headers, timeout=timeout, retries=retries)

    def get_text(self, url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
        return self.get(url, **kwargs).text(encoding)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET 并解析 JSON；``params`` 关键字会被拼进查询串。"""
        params = kwargs.pop("params", None)
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        return self.get(url, **kwargs).json()

    def post_json(
        self,
        url: str,
        payload: Any,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            merged.update(headers)
        return self.request(
            url, data=body, headers=merged, method="POST", timeout=timeout, retries=retries
        ).json()


def _retry_after_seconds(exc: urllib.error.HTTPError) -> Optional[float]:
    """读 ``Retry-After`` 头（秒数形式）。"""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None
