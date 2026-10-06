"""导出：``side-report.json`` / ``side-report.csv`` / ``side-report.txt``。

其中 ``side-report.json`` 是**与 AutoSync 的联动契约**，格式严格按
``HANDOFF.md`` 4.2 节：顶层 ``generated`` / ``tool`` / ``mods``，每个 mod 至少含
``file`` / ``sha1`` / ``sha256`` / ``mod_id`` / ``display_name`` / ``side`` /
``confidence`` / ``sources`` / ``notes``（本项目额外多写了一些字段，属超集，
不影响 AutoSync 解析）。

一键分类的目录名也与 AutoSync 的 ``classify`` 语义对齐：
``client-mods/`` / ``server-mods/`` / ``both-mods/``。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import TOOL_ID
from .cache import now_iso
from .detector import ModResult, ScanReport
from .verdict import CONFIDENCE_LABELS, SIDE_BOTH, SIDE_CLIENT, SIDE_LABELS, SIDE_SERVER, SIDE_UNKNOWN

__all__ = [
    "JSON_NAME",
    "CSV_NAME",
    "TXT_NAME",
    "CLASSIFY_DIRS",
    "write_json",
    "write_csv",
    "write_txt",
    "write_all",
    "autosync_payload",
    "side_report_payload",
    "classify_copy",
    "ClassifyCopyResult",
]

JSON_NAME = "side-report.json"
CSV_NAME = "side-report.csv"
TXT_NAME = "side-report.txt"

#: 与 AutoSync ``classify`` 对齐的输出目录名
CLASSIFY_DIRS = {
    SIDE_CLIENT: "client-mods",
    SIDE_SERVER: "server-mods",
    SIDE_BOTH: "both-mods",
}
#: ``unknown`` 归入哪个目录：**保守放进 both-mods**（两边都要装）
UNKNOWN_DIR = CLASSIFY_DIRS[SIDE_BOTH]

CSV_HEADERS = (
    "文件名",
    "判定侧别",
    "置信度",
    "冲突",
    "需人工确认",
    "已人工确认",
    "modId",
    "显示名",
    "版本",
    "主要依据",
    "来源明细",
    "备注",
    "SHA1",
)


# ---------------------------------------------------------------- JSON
def side_report_payload(report: ScanReport) -> Dict[str, Any]:
    """按 HANDOFF 4.2 节生成 ``side-report.json`` 的内容。"""
    mods: List[Dict[str, Any]] = []
    for mod in report.mods:
        mods.append(
            {
                # --- 契约字段（AutoSync 读取的） ---
                "file": mod.name,
                "sha1": mod.sha1,
                "sha256": mod.sha256,
                "mod_id": mod.mod_id,
                "display_name": mod.display_name,
                "side": mod.side,
                "confidence": mod.confidence,
                "sources": {key: dict(value) for key, value in mod.sources.items()},
                "notes": mod.notes,
                # --- 扩展字段（超集，向后兼容） ---
                "rel_path": mod.rel,
                "size": mod.size,
                "version": mod.version,
                "side_label": mod.side_label,
                "confidence_label": mod.confidence_label,
                "conflict": mod.conflict,
                "needs_review": mod.needs_review,
                "manual": mod.manual,
                "jij_mod_ids": list(mod.jij_mod_ids),
                "signals": dict(mod.signals),
            }
        )
    return {
        "generated": report.generated_at or now_iso(),
        "tool": report.tool or TOOL_ID,
        "mods_dir": report.mods_dir,
        "unknown_as": report.unknown_as,
        "counts": report.counts,
        "conflict_count": report.conflict_count,
        "review_count": report.review_count,
        "manual_count": report.manual_count,
        "source_stats": dict(report.source_stats),
        "errors": list(report.errors),
        "mods": mods,
    }


def autosync_payload(report: ScanReport) -> Dict[str, Any]:
    """AutoSync 联动的载荷（当前与 :func:`side_report_payload` 一致）。

    单独留一个函数，是为了将来 AutoSync 的读取格式变化时能独立演进 ——
    目前它严格等于 HANDOFF 4.2 节的约定。
    """
    return side_report_payload(report)


def write_json(report: ScanReport, path: Path, payload: Optional[Dict[str, Any]] = None) -> Path:
    """原子写 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload if payload is not None else side_report_payload(report)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- CSV
def _primary_evidence(mod: ModResult) -> str:
    """一行说明「主要依据」（优先人工，其次 mcmod，再次 modrinth，最后启发式）。"""
    for name in ("manual", "mcmod", "modrinth", "heuristics"):
        item = mod.sources.get(name)
        if item and str(item.get("side") or SIDE_UNKNOWN) != SIDE_UNKNOWN:
            return f"{name}: {item.get('detail', '')}"
    for name in ("mcmod", "modrinth", "heuristics"):
        item = mod.sources.get(name)
        if item:
            return f"{name}: {item.get('detail', '')}"
    return ""


def write_csv(report: ScanReport, path: Path) -> Path:
    """写 CSV（``utf-8-sig``，Excel 直接双击不乱码）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(CSV_HEADERS)
        for mod in report.mods:
            writer.writerow(
                [
                    mod.name,
                    mod.side_label,
                    mod.confidence_label,
                    "是" if mod.conflict else "",
                    "是" if mod.needs_review else "",
                    "是" if mod.manual else "",
                    mod.mod_id,
                    mod.display_name,
                    mod.version,
                    _primary_evidence(mod),
                    mod.source_summary,
                    mod.notes,
                    mod.sha1,
                ]
            )
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- TXT
def write_txt(report: ScanReport, path: Path, detail_limit: int = 500) -> Path:
    """写给人看的纯文本摘要。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = report.counts
    lines: List[str] = [
        "=" * 72,
        f"{report.tool} 侧别检测报告",
        "=" * 72,
        f"生成时间：{report.generated_at}",
        f"mods 目录：{report.mods_dir}",
        f"耗时：{report.elapsed:.1f} 秒",
        "",
        "【总览】",
        f"  共 {len(report.mods)} 个 jar",
        f"  纯客户端：{counts.get(SIDE_CLIENT, 0)}",
        f"  双端    ：{counts.get(SIDE_BOTH, 0)}",
        f"  纯服务端：{counts.get(SIDE_SERVER, 0)}",
        f"  不确定  ：{counts.get(SIDE_UNKNOWN, 0)}",
        f"  两源冲突：{report.conflict_count}    需人工确认：{report.review_count}    已人工确认：{report.manual_count}",
        "",
        "【数据源命中】",
    ]
    for name, stats in (report.source_stats or {}).items():
        if isinstance(stats, dict) and "hit" in stats:
            lines.append(
                f"  {name:<10} 命中 {stats.get('hit', 0)} / 查询 {stats.get('queried', 0)}"
                f"（占全部 {float(stats.get('hit_rate', 0)) * 100:.0f}%）"
            )
    if report.network_requests:
        lines.append(
            "  本次网络请求："
            + "，".join(f"{key} {value} 次" for key, value in report.network_requests.items())
        )
    if report.errors:
        lines.extend(["", "【查询错误】"])
        lines.extend(f"  ! {item}" for item in report.errors[:20])
        if len(report.errors) > 20:
            lines.append(f"  …（共 {len(report.errors)} 条）")

    conflicts = [mod for mod in report.mods if mod.conflict]
    if conflicts:
        lines.extend(["", f"【两源冲突（{len(conflicts)} 项，务必人工确认）】"])
        for mod in conflicts:
            lines.append(f"  [!] {mod.name}")
            lines.append(f"      {mod.source_summary}")
            lines.append(f"      {mod.notes}")

    reviews = [mod for mod in report.mods if mod.needs_review and not mod.conflict]
    if reviews:
        lines.extend(["", f"【需人工确认（{len(reviews)} 项）】"])
        for mod in reviews:
            lines.append(f"  [?] {mod.name}｜当前判定：{mod.side_label}｜{mod.notes}")

    lines.extend(["", "【按侧别分组】"])
    for side in (SIDE_CLIENT, SIDE_SERVER, SIDE_BOTH, SIDE_UNKNOWN):
        items = report.by_side(side)
        lines.append("")
        lines.append(f"--- {SIDE_LABELS.get(side, side)}（{len(items)}）---")
        for mod in items[:detail_limit]:
            flags = "".join(
                [
                    "[冲突]" if mod.conflict else "",
                    "[需确认]" if mod.needs_review else "",
                    "[人工]" if mod.manual else "",
                ]
            )
            lines.append(f"  {mod.name}{flags}")
            lines.append(f"      modId={mod.mod_id or '?'}｜置信度={mod.confidence_label}｜{mod.source_summary}")
            lines.append(f"      {mod.notes}")
        if len(items) > detail_limit:
            lines.append(f"  …（其余 {len(items) - detail_limit} 项见 CSV / JSON 报告）")

    lines.extend(["", "=" * 72])
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 一起写
def write_all(report: ScanReport, out_dir: Path, prefix: str = "side-report") -> List[Path]:
    """把三种格式都写到 ``out_dir``，返回实际写出的路径列表。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        write_json(report, out_dir / f"{prefix}.json"),
        write_csv(report, out_dir / f"{prefix}.csv"),
        write_txt(report, out_dir / f"{prefix}.txt"),
    ]


# ---------------------------------------------------------------- 一键分类
@dataclass
class ClassifyCopyResult:
    """一键分类（复制）的结果。"""

    out_dir: str = ""
    copied: Dict[str, int] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total_copied(self) -> int:
        return sum(self.copied.values())

    def summary_text(self) -> str:
        parts = [f"{name} {self.copied.get(side, 0)} 个" for side, name in CLASSIFY_DIRS.items()]
        text = "一键分类完成：" + "，".join(parts)
        if self.skipped:
            text += f"；跳过 {len(self.skipped)} 个（目标已存在同内容文件）"
        if self.conflicts:
            text += f"；冲突 {len(self.conflicts)} 个（同名但内容不同，**未覆盖**）"
        if self.errors:
            text += f"；错误 {len(self.errors)} 个"
        return text


def classify_copy(
    report: ScanReport,
    mods_dir: Path,
    out_dir: Path,
    overwrite: bool = False,
) -> ClassifyCopyResult:
    """按判定结果**复制**（不是移动）到 ``client-mods/`` / ``server-mods/`` / ``both-mods/``。

    三条安全约定：

    * 默认只复制，源文件一律不动 —— 删错了没法恢复，复制错了只是浪费磁盘；
    * 目标同名文件内容相同则跳过，内容不同则记为冲突且**绝不覆盖**（除非显式
      ``overwrite=True``）；
    * ``unknown`` 项归入 ``both-mods``（两边都要装，保守）。
    """
    mods_dir = Path(mods_dir)
    out_dir = Path(out_dir)
    result = ClassifyCopyResult(out_dir=str(out_dir))

    for side, dirname in CLASSIFY_DIRS.items():
        (out_dir / dirname).mkdir(parents=True, exist_ok=True)

    for mod in report.mods:
        source = mods_dir / mod.rel
        if not source.is_file():
            result.errors.append(f"源文件不存在：{source}")
            continue
        target_dir = out_dir / CLASSIFY_DIRS.get(mod.side, UNKNOWN_DIR)
        target = target_dir / mod.name
        try:
            if target.exists():
                if _same_file(source, target):
                    result.skipped.append(mod.name)
                    continue
                if not overwrite:
                    result.conflicts.append(f"{mod.name} -> {target_dir.name}/")
                    continue
            shutil.copy2(source, target)
            result.copied[mod.side] = result.copied.get(mod.side, 0) + 1
        except OSError as exc:
            result.errors.append(f"复制失败 {mod.name}：{exc!r}")
    return result


def _same_file(left: Path, right: Path) -> bool:
    """快速判断两个文件是否相同（先比大小，再比前 64 KB 与末尾 64 KB）。"""
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    if left_stat.st_size != right_stat.st_size:
        return False
    chunk = 64 * 1024
    try:
        with open(left, "rb") as fp_left, open(right, "rb") as fp_right:
            if fp_left.read(chunk) != fp_right.read(chunk):
                return False
            if left_stat.st_size > chunk:
                fp_left.seek(max(0, left_stat.st_size - chunk))
                fp_right.seek(max(0, right_stat.st_size - chunk))
                if fp_left.read(chunk) != fp_right.read(chunk):
                    return False
    except OSError:
        return False
    return True
