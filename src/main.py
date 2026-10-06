"""GUI 入口：``python -m src.main``。

与 CLI（``python -m src``）共用同一套 :mod:`src.detector` 逻辑，只是换了个界面。

支持的参数::

    python -m src.main                          # 打开 GUI
    python -m src.main --mods-dir "D:\\mc\\mods"  # 打开 GUI 并立即开始扫描
    python -m src.main --version                # 打印版本号后退出
    python -m src.main --self-test              # 建窗口 -> 渲染 -> 1.5 秒后自动退出（CI 用）
    python -m src.main --log-level DEBUG

``--self-test`` 是给自动化验证用的：它不扫描任何东西，只确认「窗口能建起来、
能渲染一帧、能正常销毁」，退出码 0 表示 GUI 环境可用。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 兼容「作为脚本直接运行」的场景（PyInstaller 打包出的入口、或 python src/main.py）：
# 此时没有包上下文，先把仓库根塞进 sys.path 并伪装成包内模块，
# 否则下面的相对导入会失败。
if __package__ in (None, ""):  # pragma: no cover - 只在脚本模式下命中
    import sys as _sys
    from pathlib import Path as _Path

    _root = str(_Path(__file__).resolve().parent.parent)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    __package__ = "src"

import argparse  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Optional, Sequence  # noqa: E402

from . import APP_NAME, APP_TITLE, TOOL_ID  # noqa: E402
from .paths import default_config_path  # noqa: E402

__all__ = ["main", "build_parser", "SELF_TEST_MS"]

#: ``--self-test`` 从建窗口到销毁之间的等待时间（毫秒）
SELF_TEST_MS = 1500


def _console_encoding() -> str:
    """输出统一用 UTF-8。

    Windows 控制台默认代码页是 936（GBK），而 PowerShell（尤其是被编排 / 重定向时）
    常常按 UTF-8 解码原生程序的输出 —— 两边不一致就会看到乱码。这里的策略是：

    * 一律以 UTF-8 写字节；
    * 只要碰得到控制台，就顺手把控制台输出代码页切成 65001，退出时还原，
      这样真正显示在终端上的中文也是对的。
    """
    return "utf-8"


def _set_console_code_page(code_page: int) -> bool:
    """切换控制台输出代码页（best effort）。"""
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetConsoleOutputCP(int(code_page)))
    except Exception:
        return False


#: 是否已经为「还原代码页」注册过 atexit（只注册一次）
_cp_restore_registered = False


def _ensure_utf8_console() -> bool:
    """把当前控制台切到 UTF-8 代码页（没有控制台 / 失败都只是返回 False）。"""
    global _cp_restore_registered
    if sys.platform != "win32":
        return False
    try:
        import atexit
        import ctypes

        kernel32 = ctypes.windll.kernel32
        original = int(kernel32.GetConsoleOutputCP() or 0)
        if not original:  # 压根没有控制台
            return False
        if original == 65001:
            return True
        if not _set_console_code_page(65001):
            return False
        if not _cp_restore_registered:
            # 控制台是父进程的，用完要还回去
            atexit.register(_set_console_code_page, original)
            _cp_restore_registered = True
        return True
    except Exception:
        return False


def _attach_parent_console() -> bool:
    """``--windowed`` 打包后没有控制台，``sys.stdout`` 是 ``None``。

    若本进程是从控制台（cmd / PowerShell）启动的，就附着到父进程的控制台，
    这样 ``ModSideDetector.exe --version`` 仍能看到输出；失败则安静返回。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return False
        _ensure_utf8_console()
        sys.stdout = open(  # noqa: SIM115
            "CONOUT$", "w", encoding=_console_encoding(), errors="replace", buffering=1
        )
        sys.stderr = sys.stdout
        return True
    except Exception:
        return False


def _fd1_write(text: str) -> bool:
    """直接往文件描述符 1 写。

    windowed exe 里 ``sys.stdout`` 可能是 ``None``，但 fd 1 仍可能连着父进程给的
    管道（``exe --version | Out-String``）。这条路径让重定向场景也能拿到输出；
    写不了就老实返回 False。
    """
    try:
        data = text.encode(_console_encoding(), errors="replace")
    except Exception:
        data = text.encode("utf-8", "replace")
    try:
        import os

        os.write(1, data)
        return True
    except Exception:
        return False


class _Fd1Writer:
    """``sys.stdout`` 兜底：把 print 的内容直接送进 fd 1（UTF-8）。"""

    def write(self, text: str) -> int:
        if text:
            _fd1_write(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def _std_handle_kind() -> str:
    """fd 1 当前指向什么：``disk`` / ``char``（控制台）/ ``pipe`` / ``none``。

    windowed exe 的 ``sys.stdout`` 可能是 ``None``，但操作系统层面的标准句柄可能
    仍然有效（父进程给了管道或文件）。先问清楚指向什么，再决定走哪条输出路径。
    """
    if sys.platform != "win32":
        return "none"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:  # NULL / INVALID_HANDLE_VALUE
            return "none"
        file_type = int(kernel32.GetFileType(ctypes.c_void_p(handle)))
        return {1: "disk", 2: "char", 3: "pipe"}.get(file_type, "none")
    except Exception:
        return "none"


def _force_utf8(stream: Any) -> None:
    """把已有输出流改成 UTF-8；若是控制台，先把控制台代码页也切过去。"""
    if stream is None:
        return
    try:
        if stream.isatty():
            _ensure_utf8_console()
    except Exception:
        pass
    try:
        stream.reconfigure(encoding=_console_encoding(), errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _setup_output() -> None:
    """统一输出通道（尽力而为，失败就算了）。

    windowed exe 里 ``sys.stdout`` 可能是 ``None``，也可能是 Python 用**本地代码页**
    （中文 Windows 上是 cp936）包出来的流 —— 这正是中文乱码的来源，所以两种情况
    都要处理。
    """
    if sys.stdout is not None:
        # Python 已经给了 stdout，但它多半按 cp936 编码，强制改成 UTF-8
        _force_utf8(sys.stdout)
        _force_utf8(sys.stderr)
        return
    # 输出被重定向到管道/文件：直接写 fd 1 才是对的地方（AttachConsole 会写错地方）
    if _std_handle_kind() in ("pipe", "disk"):
        sys.stdout = _Fd1Writer()  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = sys.stdout
        return
    # 从控制台启动：附着到父控制台
    if _attach_parent_console():
        return
    sys.stdout = _Fd1Writer()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _print(message: str) -> None:
    """保证在「没有控制台」的 exe 里也能把版本号之类的东西送出去，且不炸掉。"""
    if sys.stdout is not None:
        try:
            print(message)
            return
        except Exception:
            pass
    _fd1_write(message + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{APP_NAME} (GUI)",
        description=f"{APP_TITLE} —— 图形界面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            f"  python -m src.main\n"
            f"  python -m src.main --mods-dir \"D:\\mc\\mods\"\n"
            f"  python -m src.main --self-test\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_ID}")
    parser.add_argument("--mods-dir", default="", help="启动后直接扫描该目录")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help=f"自检：创建窗口、渲染一帧，{SELF_TEST_MS} 毫秒后自动关闭；成功退出码 0",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument("--config", default="", help="配置文件路径（默认 <程序目录>/config.json）")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)] if sys.stderr is not None else [],
        force=True,
    )


def _self_test(args: argparse.Namespace) -> int:
    """建窗口 -> 渲染 -> 1.5 秒后销毁。返回 0 表示 GUI 环境可用。"""
    from .ui.app import MainWindow  # 延迟导入：--version 不该拉起整个 GUI 栈

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    try:
        app = MainWindow(config_path=config_path, mods_dir=args.mods_dir or None, auto_scan=False)
        app.update_idletasks()
        app.update()  # 至少渲染一帧
        app.after(SELF_TEST_MS, app.destroy)
        app.mainloop()
    except Exception as exc:
        _print(f"[x] GUI 自检失败：{exc!r}")
        logging.getLogger(__name__).error("自检异常：\n%s", traceback.format_exc())
        return 3
    _print(f"[ok] GUI 自检通过：窗口已创建、渲染一帧并在 {SELF_TEST_MS} ms 后正常销毁。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --windowed 的 exe 没有控制台；先试 fd 1（重定向场景），再试父控制台
    _setup_output()
    _configure_logging(args.log_level)

    if args.self_test:
        return _self_test(args)

    from .ui.app import MainWindow

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    mods_dir = args.mods_dir or ""
    try:
        app = MainWindow(config_path=config_path, mods_dir=mods_dir or None, auto_scan=bool(mods_dir))
    except Exception as exc:  # pragma: no cover - 启动失败要给用户一个明确交代
        logging.getLogger(__name__).exception("GUI 启动失败")
        _print(f"[x] 无法启动图形界面：{exc!r}")
        return 3
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
