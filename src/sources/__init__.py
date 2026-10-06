"""侧别数据源。

* :mod:`~src.sources.modrinth` —— 官方 API，sha1 批量查，命中率约 80%
* :mod:`~src.sources.mcmod` —— MC 百科「运行环境」，命中率最高但需限流抓取
* :mod:`~src.sources.heuristics` —— jar 内弱信号（``displayTest`` / mixin），只作辅助
"""

from __future__ import annotations

__all__ = ["mcmod", "modrinth", "heuristics"]
