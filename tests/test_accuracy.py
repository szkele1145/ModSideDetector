"""准确率基准测试（对照人工确认集 ``tests/known_sides.json``）。

与 :mod:`tests.test_core` 不同，这个测试**不自己跑扫描** —— 它读一份已经生成的
``side-report.json``，因为含 mcmod 的全量扫描要几分钟且依赖网络，不适合放进
单元测试。用法：

.. code-block:: powershell

    # 1) 先生成报告（含 mcmod，耐心等）
    python -m src scan "C:\\...\\mods" --out-dir out
    # 2) 再校验
    python -m unittest tests.test_accuracy -v

报告路径可用环境变量 ``MSD_REPORT`` 覆盖，默认 ``out/side-report.json``。

两条断言：

1. 综合准确率必须 ≥ ``MSD_MIN_ACCURACY``（默认 0.95）—— ``PROMPT.md`` 六.1；
2. **真实侧别是 client/both 的样本，被判成 ``server`` 的数量必须为 0** ——
   ``PROMPT.md`` 六.2，代价不对等的那条铁律。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.verdict import SIDE_BOTH, SIDE_CLIENT, SIDE_SERVER  # noqa: E402

KNOWN_PATH = Path(__file__).resolve().parent / "known_sides.json"
DEFAULT_REPORT = ROOT / "out" / "side-report.json"
#: 未判定的样本（unknown）不计入准确率分母，但会单独统计
SIDE_UNKNOWN = "unknown"


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def _match_cases(mods: List[Dict[str, Any]], cases: Dict[str, str]) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """按文件名片段匹配样本，返回 ``[(片段, 期望, 实际)]`` 与未命中的片段列表。"""
    matched: List[Tuple[str, str, str]] = []
    missing: List[str] = []
    for needle, expected in cases.items():
        hits = [mod for mod in mods if needle.lower() in str(mod.get("file") or "").lower()]
        if not hits:
            missing.append(needle)
            continue
        for mod in hits:
            matched.append((needle, expected, str(mod.get("side") or SIDE_UNKNOWN)))
    return matched, missing


class TestAccuracy(unittest.TestCase):
    """对照人工确认集计算准确率。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report_path = Path(os.environ.get("MSD_REPORT") or DEFAULT_REPORT)
        if not cls.report_path.is_file():
            raise unittest.SkipTest(
                f"找不到报告 {cls.report_path}；请先运行 `python -m src scan <mods> --out-dir out`"
            )
        payload = _load_json(cls.report_path)
        cls.mods = list(payload.get("mods") or [])
        cases_payload = _load_json(KNOWN_PATH)
        cls.cases = dict(cases_payload.get("cases") or {})
        cls.matched, cls.missing = _match_cases(cls.mods, cls.cases)

    def test_have_enough_samples(self) -> None:
        """基准集至少要匹配上 15 个样本，否则「准确率」没有意义。"""
        self.assertGreaterEqual(
            len(self.matched),
            15,
            f"只匹配到 {len(self.matched)} 个基准样本（未匹配：{self.missing}）",
        )

    def test_no_false_server(self) -> None:
        """**铁律**：真实为 client/both 的 mod，一个都不许被判成 server。"""
        offenders = [
            f"{needle}（期望 {expected}，实际 {actual}）"
            for needle, expected, actual in self.matched
            if actual == SIDE_SERVER and expected != SIDE_SERVER
        ]
        self.assertEqual(offenders, [], "出现误判为纯服务端的样本：" + "；".join(offenders))

    def test_accuracy(self) -> None:
        """综合准确率 ≥ 阈值（默认 0.95）。"""
        judged = [
            (needle, expected, actual)
            for needle, expected, actual in self.matched
            if actual != SIDE_UNKNOWN
        ]
        self.assertGreater(len(judged), 0, "没有任何样本被判定，无法计算准确率")
        correct = [item for item in judged if item[1] == item[2]]
        accuracy = len(correct) / len(judged)
        threshold = float(os.environ.get("MSD_MIN_ACCURACY") or 0.95)
        wrong = [
            f"{needle}: 期望 {expected} / 实际 {actual}"
            for needle, expected, actual in judged
            if expected != actual
        ]
        self.assertGreaterEqual(
            accuracy,
            threshold,
            f"准确率 {accuracy:.1%}（{len(correct)}/{len(judged)}）低于阈值 {threshold:.0%}；"
            f"错判清单：{wrong}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
