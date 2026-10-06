"""配置：``config.json``（与 AutoSync 风格一致，UTF-8、中文注释友好）。

配置对象只做「取值 + 兜底」，不做路径校验；路径解析在 :mod:`src.paths` 里。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["Config", "DEFAULT_USER_AGENT", "load_config", "save_config"]

#: 抓 mcmod 用的 UA —— 不伪装成浏览器会被挡，实测这个可用
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class Config:
    """全部可调项。字段名即 ``config.json`` 的键名。"""

    # --- 路径 ---------------------------------------------------------
    #: 上次扫描的 mods 目录（GUI 会记住）
    mods_dir: str = ""
    #: 导出目录；留空表示与 mods 目录同级
    output_dir: str = ""
    #: 缓存目录；留空表示 <程序目录>/cache
    cache_dir: str = ""

    # --- 数据源开关 ---------------------------------------------------
    use_modrinth: bool = True
    use_mcmod: bool = True
    use_heuristics: bool = True
    #: mcmod 查询范围：
    #: ``all``（默认）= 每个 mod 都查 MC 百科，拿到完整的双源交叉验证；
    #: ``missing`` = 只查 Modrinth 没给出有效结论的那些（约 1/5），快得多，
    #: 但放弃了一部分交叉验证 —— 这是**用户可选**的加速开关，不是默认行为。
    mcmod_scope: str = "all"

    # --- mcmod --------------------------------------------------------
    #: 抓取间隔（秒）。实测 0.6 秒/请求安全，**不要并发**
    mcmod_min_interval: float = 0.6
    mcmod_timeout: float = 20.0
    mcmod_max_retries: int = 3
    mcmod_base: str = "https://www.mcmod.cn"

    # --- Modrinth -----------------------------------------------------
    modrinth_api_base: str = "https://api.modrinth.com/v2"
    modrinth_batch_size: int = 100
    modrinth_timeout: float = 20.0
    modrinth_max_retries: int = 3

    # --- 通用网络 -----------------------------------------------------
    user_agent: str = DEFAULT_USER_AGENT
    #: HTTP 代理，形如 ``http://127.0.0.1:7890``；留空走直连
    proxy: str = ""

    # --- 判定策略 -----------------------------------------------------
    #: 无法判定时按什么处理。**只允许 both / unknown**；写成 server 会被强制纠正
    unknown_as: str = "both"
    #: 缓存有效期（小时）；<=0 表示永不过期
    cache_ttl_hours: float = 720.0
    #: jar 元数据缓存（含哈希）是否复用。False 则每次重新计算哈希
    reuse_jar_hash: bool = True

    # --- AutoSync 网络上报（MSFP / 裸 TCP，见 src/upload.py）-----------
    #: 开关。**默认关** —— 这个功能不强制使用
    autosync_report_enabled: bool = False
    #: AutoSync 地址（域名或 IP；经 frp 映射到公网）
    autosync_host: str = ""
    #: AutoSync 的 MSFP 端口
    autosync_port: int = 8123
    #: 共享令牌（单行、不含空格）
    autosync_token: str = ""
    #: 连接 / 读写超时（秒）
    autosync_timeout: float = 30.0

    # --- 界面 ---------------------------------------------------------
    appearance: str = "dark"  # dark | light | system
    #: 结果表格一次最多渲染多少行（避免上万行时卡顿）
    ui_max_rows: int = 2000

    def __post_init__(self) -> None:
        # 铁律：不确定项绝不允许落到 server（会让客户端缺 mod 崩游戏）
        if str(self.unknown_as).strip().lower() not in ("both", "unknown"):
            self.unknown_as = "both"
        # mcmod 查询范围只认两个值，写错就退回默认的完整交叉验证
        if str(self.mcmod_scope).strip().lower() not in ("all", "missing"):
            self.mcmod_scope = "all"
        self.mcmod_scope = str(self.mcmod_scope).strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Config":
        config = cls()
        if not isinstance(data, dict):
            return config
        known = {item.name for item in fields(cls)}
        for key, value in data.items():
            if key not in known:
                continue
            current = getattr(config, key)
            try:
                if isinstance(current, bool):
                    setattr(config, key, bool(value))
                elif isinstance(current, int) and not isinstance(current, bool):
                    setattr(config, key, int(value))
                elif isinstance(current, float):
                    setattr(config, key, float(value))
                else:
                    setattr(config, key, "" if value is None else str(value))
            except (TypeError, ValueError):
                continue
        config.__post_init__()
        return config


def load_config(path: Path) -> Config:
    """读配置；文件不存在或损坏时返回默认配置（不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return Config()
    return Config.from_dict(data)


def save_config(path: Path, config: Config) -> None:
    """原子写配置（先写 ``.tmp`` 再替换）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(config.to_dict(), fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
