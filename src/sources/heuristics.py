"""启发式弱信号：只读 jar 内的静态特征。

**HERITAGE / 警告**：这里的一切都**不能单独定案**。

HANDOFF 实测结论：

* ``displayTest`` 字段 **93% 未填**（144 个 jar 里 134 个没写），只能当弱信号；
* **字节码扫描准确率仅约 55%**（``Axiom`` / ``Flashback`` / ``Xaero 小地图``
  全被误判），所以本项目**不做字节码扫描**，只保留元数据层面的特征；
* ``assets/`` 存在与否区分度极低（几乎所有 NeoForge mod 都有）—— 只记录不计分。

真正可靠的启发式只有两条：``clientSideOnly=true``（Forge 明确声明）与
Fabric 的 ``environment`` 字段 —— 这两个是**作者显式声明**，不是猜的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..jarinfo import JarInfo
from ..verdict import (
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    SIDE_BOTH,
    SIDE_CLIENT,
    SIDE_SERVER,
    SIDE_UNKNOWN,
    SideVerdict,
)

__all__ = ["evaluate", "display_test_hint", "HeuristicSignals"]


def display_test_hint(value: str) -> str:
    """把 ``displayTest`` 翻成人话。**不给出侧别判定**。

    语义上 ``IGNORE_ALL_VERSION`` 表示「客户端不装也能连」，纯客户端和纯服务端
    都可能这么写（AutoSync 因此把它判为 unknown）—— 所以这里只生成提示文字，
    绝不据此改判，否则会把纯服务端 mod 误判成纯客户端。
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text == "IGNORE_ALL_VERSION":
        return 'displayTest="IGNORE_ALL_VERSION"（客户端可不装，但无法区分纯客户端/纯服务端）'
    if text == "IGNORE_SERVER_VERSION":
        return 'displayTest="IGNORE_SERVER_VERSION"（忽略服务端版本差异，弱倾向客户端）'
    if text == "MATCH_VERSION":
        return 'displayTest="MATCH_VERSION"（两端版本必须一致，无侧别含义）'
    if text == "NONE":
        return 'displayTest="NONE"（不做版本检查，无侧别含义）'
    return f'displayTest="{text}"'


@dataclass
class HeuristicSignals:
    """汇总的特征，报告里原样展示（透明度优先）。"""

    display_test: str = ""
    display_test_hint: str = ""
    toml_side: Optional[str] = None
    toml_side_detail: str = ""
    has_client_mixin: bool = False
    has_server_mixin: bool = False
    mixin_configs: List[str] = None  # type: ignore[assignment]
    has_assets: bool = False
    has_data: bool = False
    mod_ids: List[str] = None  # type: ignore[assignment]
    parse_error: str = ""

    def __post_init__(self) -> None:
        if self.mixin_configs is None:
            self.mixin_configs = []
        if self.mod_ids is None:
            self.mod_ids = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "display_test": self.display_test,
            "toml_side": self.toml_side,
            "has_client_mixin": self.has_client_mixin,
            "has_server_mixin": self.has_server_mixin,
            "has_assets": self.has_assets,
            "has_data": self.has_data,
        }


def collect(jar: JarInfo) -> HeuristicSignals:
    """从 :class:`~src.jarinfo.JarInfo` 收集全部弱信号。"""
    return HeuristicSignals(
        display_test=jar.display_test,
        display_test_hint=display_test_hint(jar.display_test),
        toml_side=jar.toml_side,
        toml_side_detail=jar.toml_side_detail,
        has_client_mixin=jar.has_client_mixin,
        has_server_mixin=jar.has_server_mixin,
        mixin_configs=list(jar.mixin_configs),
        has_assets=jar.has_assets,
        has_data=jar.has_data,
        mod_ids=list(jar.mod_ids),
        parse_error=jar.parse_error,
    )


def evaluate(jar: JarInfo) -> SideVerdict:
    """给出一个**弱**判定。

    只有「显式声明」（``clientSideOnly`` / Fabric ``environment``）才给到
    ``medium`` 以上置信度，其余一律 ``low`` —— 调用方（:mod:`src.detector`）
    不会让弱信号单独定案，只有两个主源都没数据时才拿来兜底。
    """
    signals = collect(jar)

    if signals.toml_side == "client":
        return SideVerdict(
            side=SIDE_CLIENT,
            confidence=CONF_HIGH,
            source="heuristics",
            detail=signals.toml_side_detail or "元数据显式声明纯客户端",
            raw=signals.to_dict(),
        )
    if signals.toml_side == "server":
        return SideVerdict(
            side=SIDE_SERVER,
            confidence=CONF_HIGH,
            source="heuristics",
            detail=signals.toml_side_detail or "元数据显式声明纯服务端",
            raw=signals.to_dict(),
        )

    notes: List[str] = []
    if signals.display_test_hint:
        notes.append(signals.display_test_hint)
    if signals.has_client_mixin and signals.has_server_mixin:
        notes.append("mixin 配置两端都有")
    elif signals.has_client_mixin:
        notes.append("mixin 配置只含 client 段")
    elif signals.has_server_mixin:
        notes.append("mixin 配置只含 server 段")
    if signals.has_data:
        notes.append("含 data/（有服务端数据包内容）")

    weak = bool(notes)
    if weak:
        # mixin / data 这类特征**不足以判单侧**：HANDOFF 实测里 Xaero 小地图
        # （纯客户端）与一批双端 mod 的 mixin 都只有 client 段，凭它判 client
        # 会让服务端缺 mod（同样崩游戏）。所以只用来「确认不是完全没线索」，
        # 结论仍然是保守的双端。
        return SideVerdict(
            side=SIDE_BOTH,
            confidence=CONF_LOW,
            source="heuristics",
            detail="；".join(notes) + "；弱信号不足以判单侧，保守判双端",
            raw=signals.to_dict(),
        )

    detail = signals.parse_error or "jar 内无可用侧别特征"
    return SideVerdict.unknown("heuristics", detail, signals.to_dict())
