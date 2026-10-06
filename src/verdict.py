"""侧别判定结果的公共结构。

所有数据源都产出 :class:`SideVerdict`，由 :mod:`src.detector` 做多源融合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = [
    "SIDE_CLIENT",
    "SIDE_SERVER",
    "SIDE_BOTH",
    "SIDE_UNKNOWN",
    "SIDES",
    "SIDE_LABELS",
    "CONF_HIGH",
    "CONF_MEDIUM",
    "CONF_LOW",
    "CONFIDENCE_LABELS",
    "SideVerdict",
    "normalize_side",
    "side_label",
]

SIDE_CLIENT = "client"
SIDE_SERVER = "server"
SIDE_BOTH = "both"
SIDE_UNKNOWN = "unknown"

SIDES = (SIDE_CLIENT, SIDE_SERVER, SIDE_BOTH, SIDE_UNKNOWN)

SIDE_LABELS = {
    SIDE_CLIENT: "纯客户端",
    SIDE_SERVER: "纯服务端",
    SIDE_BOTH: "双端",
    SIDE_UNKNOWN: "不确定",
}

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

CONFIDENCE_LABELS = {
    CONF_HIGH: "高",
    CONF_MEDIUM: "中",
    CONF_LOW: "低",
}

#: 排序用权重：判定越「危险」（纯服务端）越靠前
SIDE_ORDER = {SIDE_SERVER: 0, SIDE_CLIENT: 1, SIDE_BOTH: 2, SIDE_UNKNOWN: 3}


def normalize_side(value: Any, default: str = SIDE_UNKNOWN) -> str:
    """把任意输入规整成四个枚举之一。"""
    text = str(value or "").strip().lower()
    aliases = {
        "client": SIDE_CLIENT,
        "client_only": SIDE_CLIENT,
        "clientonly": SIDE_CLIENT,
        "客户端": SIDE_CLIENT,
        "纯客户端": SIDE_CLIENT,
        "server": SIDE_SERVER,
        "server_only": SIDE_SERVER,
        "serveronly": SIDE_SERVER,
        "服务端": SIDE_SERVER,
        "纯服务端": SIDE_SERVER,
        "both": SIDE_BOTH,
        "双端": SIDE_BOTH,
        "unknown": SIDE_UNKNOWN,
        "不确定": SIDE_UNKNOWN,
        "": SIDE_UNKNOWN,
    }
    return aliases.get(text, default)


def side_label(side: str) -> str:
    return SIDE_LABELS.get(side, str(side))


@dataclass
class SideVerdict:
    """一个数据源对某个 jar 的判定。"""

    side: str = SIDE_UNKNOWN
    confidence: str = CONF_LOW
    #: 数据源标识：``modrinth`` / ``mcmod`` / ``heuristics`` / ``manual``
    source: str = ""
    #: 人话依据（写进报告，便于复核 —— AutoSync 上次事故就是因为看不到原始依据）
    detail: str = ""
    #: 原始字段，原样保留（例如 Modrinth 的 client_side / server_side）
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """是否给出了有效判定（unknown 视为没判）。"""
        return self.side in (SIDE_CLIENT, SIDE_SERVER, SIDE_BOTH)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "confidence": self.confidence,
            "source": self.source,
            "detail": self.detail,
            "raw": dict(self.raw),
        }

    @classmethod
    def unknown(cls, source: str, detail: str, raw: Optional[Dict[str, Any]] = None) -> "SideVerdict":
        return cls(side=SIDE_UNKNOWN, confidence=CONF_LOW, source=source, detail=detail, raw=dict(raw or {}))
