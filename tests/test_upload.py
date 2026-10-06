"""上报模块单元测试（标准库 unittest，无需第三方依赖）。

覆盖 :mod:`src.upload` 最容易出事的几点：

1. ``REPORT <token> <length>`` 的 ``<length>`` 必须是 **UTF-8 字节数**（中文报告按
   字符数算就会短一截，对方读到半个 JSON）；
2. TCP 半包 —— 响应被拆成两次 ``send`` 时仍要能解析完整一行；
3. 各种失败（令牌为空 / 连不上 / 对方回 ERR）都必须变成
   :class:`~src.upload.UploadResult`，**绝不抛异常给调用方**。

测试全部走本地假服务端（``127.0.0.1`` 随机端口），**不连任何外部地址**。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.detector import ModResult, ScanReport  # noqa: E402
from src.report import side_report_payload  # noqa: E402
from src.upload import (  # noqa: E402
    UploadResult,
    _parse_response,
    send_report_file,
    send_side_report,
)

TOKEN = "tok-abc123"
#: 真实报告（151 条）—— 端到端验证用；没有就跳过那条用例
REAL_REPORT = ROOT / "out" / "side-report.json"


# ---------------------------------------------------------------- 假服务端
class FakeAutoSync:
    """在 ``127.0.0.1`` 随机端口上收一条 MSFP ``REPORT`` 命令的假 AutoSync。

    用法::

        with FakeAutoSync("OK 3\\n") as server:
            result = send_side_report(report, server.host, server.port, TOKEN)
        print(server.header_line, len(server.body))
    """

    def __init__(self, response: bytes = b"OK 3\n", split: bool = False, accept_timeout: float = 5.0) -> None:
        self.response = response
        self.split = split
        self.accept_timeout = accept_timeout

        self.connected = False
        self.header_line = b""
        self.body = b""
        self.error: BaseException | None = None

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, name="FakeAutoSync", daemon=True)

    # ---- 上下文管理 ----
    def __enter__(self) -> "FakeAutoSync":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.finish()

    def finish(self, timeout: float = 5.0) -> None:
        """等假服务端收完这一条（或确认没人连过来），然后关掉监听。"""
        self._thread.join(timeout=timeout)
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass

    # ---- 服务端逻辑 ----
    def _serve(self) -> None:
        try:
            self._sock.settimeout(self.accept_timeout)
            conn, _addr = self._sock.accept()
        except OSError:
            return  # 没人连过来（例如「令牌为空不该发请求」那条用例）
        self.connected = True

        try:
            with conn:
                conn.settimeout(5.0)
                fp = conn.makefile("rb")
                # 第一行：REPORT <token> <length>\n
                self.header_line = fp.readline()
                parts = self.header_line.decode("utf-8").split()
                length = int(parts[2])
                # 第二行（body）：正好 length 字节
                self.body = fp.read(length)

                data = self.response
                if self.split and len(data) > 1:
                    # 拆两次发，模拟 TCP 半包（第一次不带换行）
                    mid = max(1, len(data) // 2)
                    conn.sendall(data[:mid])
                    time.sleep(0.05)
                    conn.sendall(data[mid:])
                else:
                    conn.sendall(data)
        except BaseException as exc:  # pragma: no cover - 出错要能让测试看见
            self.error = exc


# ---------------------------------------------------------------- 造数据
def _report(mods: int = 3) -> ScanReport:
    """造一个含中文的报告 —— 专门用来验证「长度按字节算」。"""
    report = ScanReport()
    report.generated_at = "2026-10-06T12:00:00+08:00"
    report.tool = "ModSideDetector"
    report.mods_dir = r"D:\mc\mods"
    report.mods = [
        ModResult(
            rel=f"模组{i}.jar",
            name=f"[中文名]测试模组{i}.jar",
            display_name=f"测试模组{i}",
            mod_id=f"testmod{i}",
            side="both",
            confidence="high",
            notes="中文备注：用于验证 UTF-8 字节长度",
        )
        for i in range(mods)
    ]
    return report


def _expected_body(report: ScanReport) -> bytes:
    return json.dumps(side_report_payload(report), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- 协议
class ReportCommandTests(unittest.TestCase):
    """命令头 / 长度 / 响应解析。"""

    def test_command_header_and_utf8_length(self) -> None:
        """前两行严格等于 ``REPORT <token> <length>\\n`` + 恰好 length 字节的 JSON。"""
        report = _report(3)
        expected = _expected_body(report)

        with FakeAutoSync(b"OK 3\n") as server:
            result = send_side_report(report, server.host, server.port, TOKEN, timeout=5.0)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.count, 3)

        # 第一行：命令头（严格比对）
        self.assertEqual(server.header_line, f"REPORT {TOKEN} {len(expected)}\n".encode("utf-8"))
        # 第二行：body 字节与 UTF-8 序列化结果**完全一致**
        self.assertEqual(server.body, expected)
        # length 必须等于字节数，而不是字符数（中文报告两者差很多）
        char_count = len(expected.decode("utf-8"))
        self.assertNotEqual(len(expected), char_count)
        self.assertEqual(server.header_line.decode("utf-8").split()[2], str(len(expected)))

    def test_ok_with_count(self) -> None:
        """``OK 3`` -> ``ok=True, count=3``。"""
        with FakeAutoSync(b"OK 3\n") as server:
            result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
        self.assertEqual((result.ok, result.count), (True, 3))
        self.assertEqual(result.raw_response.strip(), "OK 3")
        self.assertIn("3", result.message)

    def test_half_packet_response(self) -> None:
        """响应被拆成两次 send 时仍要解析正确（不能假设一次 recv 拿到整行）。"""
        with FakeAutoSync(b"OK 151\n", split=True) as server:
            result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.count, 151)
        self.assertEqual(result.raw_response, "OK 151\n")

    def test_err_unauthorized_is_human_readable(self) -> None:
        with FakeAutoSync(b"ERR unauthorized\n") as server:
            result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
        self.assertFalse(result.ok)
        self.assertIn("令牌", result.message)
        self.assertEqual(result.count, 0)
        self.assertEqual(result.raw_response.strip(), "ERR unauthorized")

    def test_err_disabled_mapping(self) -> None:
        with FakeAutoSync(b"ERR disabled\n") as server:
            result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
        self.assertFalse(result.ok)
        self.assertIn("未启用", result.message)

    def test_err_too_large_and_bad_request(self) -> None:
        for raw, keyword in ((b"ERR too large\n", "大"), (b"ERR bad request\n", "格式")):
            with self.subTest(raw=raw):
                with FakeAutoSync(raw) as server:
                    result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
                self.assertFalse(result.ok)
                self.assertIn(keyword, result.message)

    def test_err_internal_keeps_detail(self) -> None:
        with FakeAutoSync(b"ERR internal disk full\n") as server:
            result = send_side_report(_report(), server.host, server.port, TOKEN, timeout=5.0)
        self.assertFalse(result.ok)
        self.assertIn("disk full", result.message)

    def test_parse_response_unknown_line(self) -> None:
        result = _parse_response("WAT 1 2\n")
        self.assertFalse(result.ok)
        self.assertIn("WAT", result.message)

    def test_parse_response_empty(self) -> None:
        result = _parse_response("")
        self.assertFalse(result.ok)
        self.assertTrue(result.message)


# ---------------------------------------------------------------- 失败路径
class UploadFailureTests(unittest.TestCase):
    """失败一律返回 UploadResult，绝不抛异常。"""

    def test_empty_token_sends_nothing(self) -> None:
        """令牌为空：直接失败，**不发起任何连接**。"""
        with FakeAutoSync(b"OK 3\n", accept_timeout=0.6) as server:
            result = send_side_report(_report(), server.host, server.port, "", timeout=5.0)
            server.finish()
            self.assertFalse(server.connected, "令牌为空时不该建立连接")
        self.assertFalse(result.ok)
        self.assertIn("令牌", result.message)
        self.assertEqual(result.raw_response, "")

    def test_empty_host_fails(self) -> None:
        result = send_side_report(_report(), "", 8123, TOKEN, timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIn("地址", result.message)

    def test_token_with_space_fails(self) -> None:
        result = send_side_report(_report(), "127.0.0.1", 1, "bad token", timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIn("空格", result.message)

    def test_bad_port_fails(self) -> None:
        result = send_side_report(_report(), "127.0.0.1", 70000, TOKEN, timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIn("端口", result.message)

    def test_connection_refused_returns_failure(self) -> None:
        """连不上的端口（127.0.0.1:1）返回失败而不是抛异常。"""
        try:
            result = send_side_report(_report(), "127.0.0.1", 1, TOKEN, timeout=2.0)
        except Exception as exc:  # pragma: no cover - 这条分支出现即失败
            self.fail(f"上报不该抛异常，却抛了：{exc!r}")
        self.assertIsInstance(result, UploadResult)
        self.assertFalse(result.ok)
        self.assertTrue(result.message)

    def test_unresolvable_host_returns_failure(self) -> None:
        result = send_side_report(
            _report(), "no-such-host.invalid", 8123, TOKEN, timeout=2.0
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.message)

    def test_missing_file_returns_failure(self) -> None:
        result = send_report_file(ROOT / "out" / "不存在的报告.json", "127.0.0.1", 1, TOKEN)
        self.assertFalse(result.ok)
        self.assertIn("找不到", result.message)


# ---------------------------------------------------------------- 文件上报 / 端到端
class ReportFileTests(unittest.TestCase):
    def test_send_report_file(self) -> None:
        report = _report(2)
        tmp = ROOT / "out" / "_test_upload_tmp.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text(
                json.dumps(side_report_payload(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with FakeAutoSync(b"OK 2\n") as server:
                result = send_report_file(tmp, server.host, server.port, TOKEN, timeout=5.0)
            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.count, 2)
            sent = json.loads(server.body.decode("utf-8"))
            self.assertEqual(len(sent["mods"]), 2)
            self.assertEqual(server.body, _expected_body(report))
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_invalid_json_file(self) -> None:
        tmp = ROOT / "out" / "_test_upload_bad.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text("{ 这不是 JSON", encoding="utf-8")
            result = send_report_file(tmp, "127.0.0.1", 1, TOKEN)
            self.assertFalse(result.ok)
            self.assertIn("JSON", result.message)
        finally:
            if tmp.exists():
                tmp.unlink()

    @unittest.skipUnless(REAL_REPORT.is_file(), f"缺少真实报告 {REAL_REPORT}")
    def test_real_side_report_round_trip(self) -> None:
        """端到端：真实 ``out/side-report.json`` 走一遍完整协议。"""
        expected_mods = len(json.loads(REAL_REPORT.read_text(encoding="utf-8"))["mods"])
        real_bytes = json.dumps(
            json.loads(REAL_REPORT.read_text(encoding="utf-8")), ensure_ascii=False
        ).encode("utf-8")

        with FakeAutoSync(f"OK {expected_mods}\n".encode("utf-8")) as server:
            result = send_report_file(REAL_REPORT, server.host, server.port, TOKEN, timeout=5.0)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.count, expected_mods)
        self.assertEqual(server.header_line, f"REPORT {TOKEN} {len(real_bytes)}\n".encode("utf-8"))
        self.assertEqual(len(server.body), len(real_bytes))
        self.assertEqual(len(json.loads(server.body.decode("utf-8"))["mods"]), expected_mods)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
