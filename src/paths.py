"""路径工具：定位程序目录、缓存目录、导出目录。

PyInstaller 打包后 ``__file__`` 指向临时解包目录（``_MEIPASS``），配置与缓存
**绝不能**写在那里（进程退出即消失，而且别的机器上未必可写）。所以：

* 冻结运行时：程序目录 = exe 所在目录；
* 源码运行时：程序目录 = 仓库根（``src`` 的上一级）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["is_frozen", "app_dir", "repo_root", "default_config_path", "resolve_cache_dir", "resolve_output_dir"]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结环境里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """配置 / 缓存的默认落点。

    冻结时用 exe 所在目录（``sys.executable`` 的父目录），源码运行时用仓库根。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """源码仓库根目录（打包后等同于 :func:`app_dir`）。"""
    return app_dir() if is_frozen() else Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    """``config.json`` 的位置：优先 exe/仓库根，其次用户主目录。"""
    candidate = app_dir() / "config.json"
    try:
        if os.access(app_dir(), os.W_OK):
            return candidate
    except OSError:
        pass
    return Path.home() / ".modsidedetector" / "config.json"


def resolve_cache_dir(config_dir: str, base: Path = None) -> Path:
    """解析缓存目录：配置为空时用 ``<程序目录>/cache``。

    ⚠️ 缓存里可能含有 mcmod 抓取内容（BY-NC-SA 3.0），**不要随 exe 一起分发**。
    """
    base = app_dir() if base is None else Path(base)
    raw = str(config_dir or "").strip()
    if not raw:
        return base / "cache"
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (base / path)


def resolve_output_dir(config_value: str, mods_dir: Path, base: Path = None) -> Path:
    """解析导出目录：配置为空时用 mods 目录本身（报告与 mods 放一起最直观）。"""
    base = app_dir() if base is None else Path(base)
    raw = str(config_value or "").strip()
    if not raw:
        return Path(mods_dir)
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (base / path)
