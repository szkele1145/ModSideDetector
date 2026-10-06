"""上报：把侧别报告通过裸 TCP 发给 AutoSync（MSFP 协议的 ``REPORT`` 命令）。

**为什么不用 HTTP**：AutoSync 跑在阿里云大陆节点，备案拦截系统会拦 HTTP 流量；
裸 TCP（经 frp 隧道映射公网，默认端口 8123）不受影响。

协议（单行命令 + 定长 body）::

    REPORT <token> <length>\\n
    <length 字节的 UTF-8 JSON>

响应（单行，``\\n`` 结尾）::

    OK <count>\\n          成功，count = 本次报告的 mods 条目数
    ERR unauthorized\\n   令牌不对
    ERR disabled\\n       对方没启用该功能 / 没配令牌
    ERR too large\\n      报告体积超过对方限制
    ERR bad request\\n    请求格式不对（多半是协议版本不一致）
    ERR internal <detail>\\n  对方内部错误

三条硬约束（都有对应单元测试）：

1. ``<length>`` 必须是 **UTF-8 字节数**，不是字符数 —— 中文报告按字符数算必错；
2. 读响应不能假设一次 ``recv`` 就能拿到整行（TCP 半包），用 ``makefile("rb").readline()``；
3. **任何失败都不抛异常给调用方** —— 上报失败绝不能让扫描流程失败。
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .detector import ScanReport
from .report import side_report_payload

__all__ = [
    "UploadResult",
    "send_side_report",
    "send_report_file",
    "DEFAULT_TIMEOUT",
    "DEFAULT_PORT",
]

#: AutoSync 侧 MSFP 端口默认值（经 frp 映射到公网）
DEFAULT_PORT = 8123
#: 默认连接 / 读写超时（秒）
DEFAULT_TIMEOUT = 30.0

#: ``ERR xxx`` -> 给人看的中文解释
ERR_MESSAGES: Dict[str, str] = {
    "unauthorized": "对方拒绝：令牌（token）不正确",
    "disabled": "对方未启用该功能或未配置令牌",
    "too large": "报告太大，超过对方限制",
    "bad request": "对方认为请求格式不对（协议不匹配？）",
}


@dataclass
class UploadResult:
    """上报结果。**永远**由函数返回，不通过异常传递。"""

    ok: bool
    #: 给人看的中文说明（成功或失败原因）
    message: str
    #: ``OK`` 时对方返回的条目数
    count: int = 0
    #: 原始响应行（排查用；失败时可能为空）
    raw_response: str = ""

    def summary_text(self) -> str:
        """一行式摘要，CLI / GUI 直接可用。"""
        return self.message


# ---------------------------------------------------------------- 响应解析
def _parse_response(raw: str) -> UploadResult:
    """把对方的一行响应解析成 :class:`UploadResult`。"""
    line = (raw or "").strip()
    if not line:
        return UploadResult(False, "对方没有返回任何内容（连接可能被直接关闭）", raw_response=raw)

    upper = line.upper()
    if upper == "OK" or upper.startswith("OK "):
        rest = line[2:].strip()
        count = 0
        if rest:
            try:
                count = int(rest.split()[0])
            except (TypeError, ValueError):
                # 回了 OK 但数字看不懂：仍算成功，只是拿不到条目数
                return UploadResult(
                    True,
                    f"上报成功，但对方返回的条目数无法解析：{line}",
                    count=0,
                    raw_response=raw,
                )
        return UploadResult(True, f"上报成功：对方已接收 {count} 条 mod 记录", count=count, raw_response=raw)

    if upper == "ERR" or upper.startswith("ERR "):
        detail = line[3:].strip()
        key = detail.lower()
        # ERR internal <detail>：把对方的 detail 原样带出来，便于排查
        if key.startswith("internal"):
            extra = detail[len("internal") :].strip()
            message = "对方内部错误" + (f"：{extra}" if extra else "")
            return UploadResult(False, message, raw_response=raw)
        message = ERR_MESSAGES.get(key)
        if message is None:
            message = f"对方返回错误：{detail or line}"
        return UploadResult(False, message, raw_response=raw)

    return UploadResult(False, f"无法识别的响应：{line}", raw_response=raw)


# ---------------------------------------------------------------- 发送
def _connection_error(phase: str, host: str, port: int, timeout: float, exc: BaseException) -> UploadResult:
    """把连接/读写异常翻译成人话。"""
    if isinstance(exc, TimeoutError):
        doing = "连接" if phase == "connect" else "读取响应"
        return UploadResult(False, f"{doing}超时：{host}:{port}（{timeout:g} 秒）")
    if isinstance(exc, socket.gaierror):
        return UploadResult(False, f"无法解析地址「{host}」：{exc}")
    if isinstance(exc, ConnectionRefusedError):
        return UploadResult(False, f"连接被拒绝：{host}:{port}（对方未启动或端口不对）")
    return UploadResult(False, f"网络错误：{exc!r}")


def _send_body(body: bytes, entry_count: int, host: str, port: int, token: str, timeout: float) -> UploadResult:
    """发一条 ``REPORT`` 并读回响应。所有异常都在这里被吃掉。"""
    host = str(host or "").strip()
    token = str(token or "").strip()

    # 先做本地校验：配置不全就别去连网了（也避免把空令牌发出去被记成一次失败尝试）
    if not token:
        return UploadResult(False, "未配置令牌（autosync_token 为空），已跳过上报")
    if not host:
        return UploadResult(False, "未配置 AutoSync 地址（autosync_host 为空），已跳过上报")
    if any(ch.isspace() for ch in token):
        return UploadResult(False, "令牌不能包含空格或换行（MSFP 的 token 是单行字段）")
    if not (0 < int(port) < 65536):
        return UploadResult(False, f"端口不合法：{port}（应在 1-65535 之间）")

    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT

    # 命令头与 body 分开算：<length> 一定是 body 的 UTF-8 字节数
    header = f"REPORT {token} {len(body)}\n".encode("utf-8")

    phase = "connect"
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(header)
        sock.sendall(body)
        phase = "read"
        # 半包：必须按行读到 \n 为止，绝不能假设一次 recv 拿到整行
        with sock.makefile("rb") as fp:
            raw_line = fp.readline()
    except Exception as exc:  # noqa: BLE001 —— 失败一律翻译成人话，绝不外抛
        return _connection_error(phase, host, int(port), timeout, exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass

    raw = raw_line.decode("utf-8", errors="replace")
    result = _parse_response(raw)
    if result.ok and result.count == 0 and entry_count:
        # 对方没回数字（或回了 0）时，不让调用方误以为「一条都没收」
        result.count = 0
    return result


def _dump_payload(payload: Dict[str, Any]) -> bytes:
    """按硬要求序列化：``ensure_ascii=False`` 后取 UTF-8 字节。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def send_side_report(
    report: ScanReport,
    host: str,
    port: int,
    token: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> UploadResult:
    """按 MSFP 的 ``REPORT`` 命令把报告发给 AutoSync。

    ``report`` 就是 :class:`~src.detector.ScanReport`，JSON 内容与
    ``side-report.json`` 完全一致（同一个 :func:`~src.report.side_report_payload`）。
    """
    if report is None:
        return UploadResult(False, "没有可上报的扫描结果")
    try:
        payload = side_report_payload(report)
        body = _dump_payload(payload)
    except Exception as exc:  # noqa: BLE001 - 序列化失败也不能炸扫描流程
        return UploadResult(False, f"报告序列化失败：{exc!r}")
    return _send_body(body, len(payload.get("mods") or []), host, port, token, timeout)


def send_report_file(
    path: Path,
    host: str,
    port: int,
    token: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> UploadResult:
    """把磁盘上的 ``side-report.json`` 发出去（GUI「立即上报」用这个）。"""
    path = Path(path)
    try:
        # utf-8-sig：容忍别人用记事本编辑后留下的 BOM
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return UploadResult(False, f"找不到报告文件：{path}")
    except OSError as exc:
        return UploadResult(False, f"读取报告文件失败：{path}（{exc!r}）")

    try:
        payload = json.loads(text)
    except ValueError as exc:
        return UploadResult(False, f"报告文件不是合法 JSON：{path}（{exc}）")
    if not isinstance(payload, dict):
        return UploadResult(False, f"报告文件的顶层不是 JSON 对象：{path}")

    try:
        # 统一重排一遍：既保证 <length> 与 body 严格一致，也顺手去掉 BOM
        body = _dump_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return UploadResult(False, f"报告序列化失败：{exc!r}")

    mods = payload.get("mods")
    entry_count = len(mods) if isinstance(mods, list) else 0
    return _send_body(body, entry_count, host, port, token, timeout)
