"""ModSideDetector —— Minecraft 模组运行侧别检测工具。

判定每个 mod 属于 **纯客户端** / **纯服务端** / **双端**，并导出报告与
AutoSync 联动。核心原则见 ``HANDOFF.md``：**不确定的一律按双端**，
宁可多发（浪费带宽）也绝不漏发（客户端直接崩游戏）。
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME", "APP_TITLE", "TOOL_ID"]

#: 语义化版本号；``--version`` 与报告里的 ``tool`` 字段都从这里取
__version__ = "1.0.0"

APP_NAME = "ModSideDetector"
APP_TITLE = "ModSideDetector — Minecraft 模组侧别检测"
#: 写进 side-report.json 的 ``tool`` 字段
TOOL_ID = f"{APP_NAME} {__version__}"
