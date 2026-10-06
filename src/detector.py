"""核心检测编排：扫描 jar -> 多源查询 -> 融合判定。

融合规则（``PROMPT.md`` 2.1 / ``HANDOFF.md`` 第三章）::

    两源一致       -> 采信，置信度 high
    两源冲突       -> 保守判 both，标记「需人工确认」，置信度 low
    只有一源       -> 采信，置信度 medium
    都没有         -> both（保守默认），置信度 low

**铁律**（写在代码里，不是文档里）：

* 判定为 ``server`` 必须**有明确依据**（Modrinth ``client_side=unsupported``
  或 mcmod 明写「客户端无效」），否则一律降级 ``both``；
* 冲突时不做「谁更权威」的猜测，直接取双端并请人来定 —— 猜错的代价是崩游戏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import TOOL_ID
from .cache import CacheManager, jar_cache_key, now_iso
from .config import Config
from .jarinfo import JarInfo, inspect_jar, iter_jars
from .net import HttpClient
from .sources import heuristics as heuristics_source
from .sources import mcmod as mcmod_source
from .sources import modrinth as modrinth_source
from .verdict import (
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    CONFIDENCE_LABELS,
    SIDE_BOTH,
    SIDE_CLIENT,
    SIDE_LABELS,
    SIDE_SERVER,
    SIDE_UNKNOWN,
    SIDES,
    SideVerdict,
    normalize_side,
    side_label,
)

__all__ = ["ModResult", "ScanReport", "Detector", "merge_verdicts"]

#: 进度回调签名 ``(phase, done, total, message)``
ProgressFn = Callable[[str, int, int, str], None]
#: 取消回调：返回 True 表示请求中止
CancelFn = Callable[[], bool]

#: 主数据源（决定冲突与置信度）；mcmod 质量最高但它不参与「谁更权威」的猜测
PRIMARY_SOURCES: Tuple[str, ...] = ("mcmod", "modrinth")

#: 每处理这么多个 jar 就把缓存落盘一次。
#: mcmod 是串行限流（0.6 秒/请求）的最慢环节，整个扫描要好几分钟 ——
#: 只在末尾保存的话，用户中途取消（或被中断）会让已经抓到的结果**全部白费**。
_AUTOSAVE_EVERY = 10


@dataclass
class ModResult:
    """一个 jar 的最终判定。"""

    rel: str = ""
    name: str = ""
    size: int = 0
    sha1: str = ""
    sha256: str = ""

    mod_id: str = ""
    display_name: str = ""
    version: str = ""
    jij_mod_ids: List[str] = field(default_factory=list)

    side: str = SIDE_UNKNOWN
    confidence: str = CONF_LOW
    #: 两源冲突（界面高亮）
    conflict: bool = False
    #: 需要人工确认（冲突 / 判不了 / 被降级）
    needs_review: bool = False
    #: 来自人工复核（写进缓存，下次扫描直接沿用）
    manual: bool = False

    notes: str = ""
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    signals: Dict[str, Any] = field(default_factory=dict)

    @property
    def side_label(self) -> str:
        return side_label(self.side)

    @property
    def confidence_label(self) -> str:
        return CONFIDENCE_LABELS.get(self.confidence, self.confidence)

    @property
    def source_summary(self) -> str:
        """一行展示各源结论，例如 ``mcmod=纯客户端 / modrinth=双端``。"""
        parts: List[str] = []
        for name in ("mcmod", "modrinth", "heuristics", "manual"):
            item = self.sources.get(name)
            if not item:
                continue
            parts.append(f"{name}={side_label(str(item.get('side') or SIDE_UNKNOWN))}")
        return " / ".join(parts)

    def override_key(self) -> Tuple[str, str]:
        return self.sha1, self.mod_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.name,
            "rel_path": self.rel,
            "size": self.size,
            "sha1": self.sha1,
            "sha256": self.sha256,
            "mod_id": self.mod_id,
            "display_name": self.display_name,
            "version": self.version,
            "jij_mod_ids": list(self.jij_mod_ids),
            "side": self.side,
            "side_label": self.side_label,
            "confidence": self.confidence,
            "conflict": self.conflict,
            "needs_review": self.needs_review,
            "manual": self.manual,
            "notes": self.notes,
            "sources": {key: dict(value) for key, value in self.sources.items()},
            "signals": dict(self.signals),
        }


@dataclass
class ScanReport:
    """一次完整扫描的结果。"""

    generated_at: str = ""
    tool: str = TOOL_ID
    mods_dir: str = ""
    unknown_as: str = SIDE_BOTH
    elapsed: float = 0.0
    mods: List[ModResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    #: 数据源命中统计（用于验收：各源命中率）
    source_stats: Dict[str, Any] = field(default_factory=dict)
    #: 缓存统计（第二次扫描是否真的没发请求）
    cache_stats: Dict[str, int] = field(default_factory=dict)
    #: 本次真正发出的网络请求数（按源）
    network_requests: Dict[str, int] = field(default_factory=dict)
    canceled: bool = False

    # ------------------------------------------------------------ 视图
    @property
    def counts(self) -> Dict[str, int]:
        result = {side: 0 for side in SIDES}
        for mod in self.mods:
            result[mod.side] = result.get(mod.side, 0) + 1
        return result

    @property
    def conflict_count(self) -> int:
        return sum(1 for mod in self.mods if mod.conflict)

    @property
    def review_count(self) -> int:
        return sum(1 for mod in self.mods if mod.needs_review)

    @property
    def manual_count(self) -> int:
        return sum(1 for mod in self.mods if mod.manual)

    def by_side(self, side: str) -> List[ModResult]:
        return [mod for mod in self.mods if mod.side == side]

    def summary_text(self) -> str:
        counts = self.counts
        return (
            f"共 {len(self.mods)} 个 jar："
            f"纯客户端 {counts.get(SIDE_CLIENT, 0)} / "
            f"双端 {counts.get(SIDE_BOTH, 0)} / "
            f"纯服务端 {counts.get(SIDE_SERVER, 0)} / "
            f"不确定 {counts.get(SIDE_UNKNOWN, 0)}"
            f"（冲突 {self.conflict_count}，需人工确认 {self.review_count}，已人工确认 {self.manual_count}）"
            f"，耗时 {self.elapsed:.1f}s"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated": self.generated_at,
            "tool": self.tool,
            "mods_dir": self.mods_dir,
            "unknown_as": self.unknown_as,
            "elapsed": round(self.elapsed, 3),
            "counts": self.counts,
            "conflict_count": self.conflict_count,
            "review_count": self.review_count,
            "manual_count": self.manual_count,
            "source_stats": dict(self.source_stats),
            "cache_stats": dict(self.cache_stats),
            "network_requests": dict(self.network_requests),
            "errors": list(self.errors),
            "mods": [mod.to_dict() for mod in self.mods],
        }


def _has_explicit_single_side(verdict: SideVerdict) -> bool:
    """该判定是否带**明确单侧依据**（mcmod 明写「某端无效」）。

    只有 mcmod 会产出这种依据（Modrinth 的 ``unsupported`` 走的是另一条分支）。
    「客户端需装」这半句不算 —— 它推不出单侧结论。
    """
    if verdict.confidence != CONF_HIGH:
        return False
    run_env = str((verdict.raw or {}).get("run_env") or "")
    return ("服务端无效" in run_env) or ("客户端无效" in run_env)


# ---------------------------------------------------------------- 融合
def merge_verdicts(
    jar: JarInfo,
    verdicts: Dict[str, SideVerdict],
    override: Optional[Dict[str, Any]],
    unknown_as: str = SIDE_BOTH,
) -> ModResult:
    """把各源结论融合成一个 :class:`ModResult`（纯函数，独立可测）。"""
    result = ModResult(
        rel=jar.rel,
        name=jar.name,
        size=jar.size,
        sha1=jar.sha1,
        sha256=jar.sha256,
        mod_id=jar.primary_mod_id,
        display_name=jar.display_name or jar.primary_mod_id or jar.name,
        version=jar.version,
        jij_mod_ids=list(jar.jij_mod_ids),
        sources={name: verdict.to_dict() for name, verdict in verdicts.items()},
        signals=heuristics_source.collect(jar).to_dict(),
    )
    notes: List[str] = []

    # ---------------------------------------------------------- 1) 人工复核
    if override is not None:
        side = normalize_side(override.get("side"))
        if side != SIDE_UNKNOWN:
            result.side = side
            result.confidence = CONF_HIGH
            result.manual = True
            result.needs_review = False
            note = str(override.get("note") or "")
            result.notes = (
                f"人工确认：{side_label(side)}"
                + (f"（{note}）" if note else "")
                + f"｜记录于 {override.get('at', '')}"
            )
            result.sources["manual"] = SideVerdict(
                side=side, confidence=CONF_HIGH, source="manual", detail=result.notes
            ).to_dict()
            return result

    # ---------------------------------------------------------- 2) 主源比对
    available = [
        name
        for name in PRIMARY_SOURCES
        if name in verdicts and verdicts[name].known
    ]
    sides = {name: verdicts[name].side for name in available}

    if len(available) >= 2:
        distinct = set(sides.values())
        if len(distinct) == 1:
            result.side = next(iter(distinct))
            result.confidence = CONF_HIGH
            result.notes = "两源一致：" + "、".join(
                f"{name}={side_label(sides[name])}" for name in available
            )
        else:
            mcmod_verdict = verdicts.get("mcmod")
            modrinth_verdict = verdicts.get("modrinth")
            # 特例：mcmod 有**明确**单侧依据（「服务端无效」/「客户端无效」），
            # 而 Modrinth 只是弱依据（optional 之类）—— 按 HANDOFF「mcmod 质量最高」
            # 采信 mcmod，但置信度压到 low 且强制人工确认，不做静默采信。
            if (
                mcmod_verdict is not None
                and modrinth_verdict is not None
                and _has_explicit_single_side(mcmod_verdict)
                and modrinth_verdict.confidence in (CONF_LOW, CONF_MEDIUM)
                and modrinth_verdict.side != mcmod_verdict.side
            ):
                result.side = mcmod_verdict.side
                result.confidence = CONF_LOW
                result.conflict = True
                result.needs_review = True
                notes.append(
                    "两源冲突："
                    + "、".join(f"{name}={side_label(sides[name])}" for name in available)
                    + "；mcmod 有明确依据（"
                    + str((mcmod_verdict.raw or {}).get("run_env") or "")
                    + "）而 Modrinth 依据较弱，暂采信 mcmod，请人工确认"
                )
            else:
                # 冲突：**不猜谁更权威**，保守取双端并请人来定
                result.side = SIDE_BOTH
                result.confidence = CONF_LOW
                result.conflict = True
                result.needs_review = True
                notes.append(
                    "两源冲突："
                    + "、".join(f"{name}={side_label(sides[name])}" for name in available)
                    + "；已保守判双端，请人工确认"
                )
    elif len(available) == 1:
        name = available[0]
        result.side = sides[name]
        result.confidence = CONF_MEDIUM
        notes.append(f"仅 {name} 有数据：{side_label(result.side)}（置信度中）")
    else:
        # -------------------------------------------------- 3) 弱信号兜底
        hint = verdicts.get("heuristics")
        if hint is not None and hint.known:
            result.side = hint.side
            result.confidence = hint.confidence
            result.needs_review = hint.confidence == CONF_LOW
            notes.append(f"两主源均无数据，采用启发式：{hint.detail}")
        else:
            result.side = normalize_side(unknown_as, SIDE_BOTH)
            result.confidence = CONF_LOW
            result.needs_review = True
            detail = hint.detail if hint is not None else "无任何数据源可用"
            notes.append(f"无数据源可判定（{detail}），按保守默认处理")

    # ---------------------------------------------------------- 4) 弱信号补充说明
    hint = verdicts.get("heuristics")
    if hint is not None and hint.detail and hint.detail not in ("jar 内无可用侧别特征",):
        if hint.known and hint.side != result.side and not result.conflict:
            notes.append(f"启发式提示：{hint.detail}")
        elif hint.known:
            notes.append(f"启发式一致：{hint.detail}")

    # ---------------------------------------------------------- 5) server 铁律
    if result.side == SIDE_SERVER:
        supporters = [
            verdict
            for verdict in verdicts.values()
            if verdict.side == SIDE_SERVER and verdict.confidence in (CONF_HIGH, CONF_MEDIUM)
        ]
        if not supporters:
            result.side = SIDE_BOTH
            result.confidence = CONF_LOW
            result.needs_review = True
            notes.append("原判纯服务端但缺少明确依据（client_side=unsupported / 客户端无效），已按铁律降级为双端")

    # ---------------------------------------------------------- 6) 其它提示
    if result.side == SIDE_UNKNOWN:
        result.needs_review = True
    if jar.parse_error and not result.mod_id:
        notes.append(f"jar 元数据不可用：{jar.parse_error}")
    if jar.jij_mod_ids:
        notes.append(f"自带 JiJ 前置 {len(jar.jij_mod_ids)} 个（无需单独安装）")

    result.notes = "；".join(item for item in ([result.notes] + notes) if item)
    return result


# ---------------------------------------------------------------- 编排
@dataclass
class Detector:
    """扫描 + 多源查询 + 融合。"""

    config: Config
    cache: CacheManager
    logger: Any = None

    _modrinth: Optional[modrinth_source.ModrinthSource] = field(default=None, repr=False)
    _mcmod: Optional[mcmod_source.McModSource] = field(default=None, repr=False)

    # ------------------------------------------------------------ 日志
    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if callable(handler):
            handler(message)
        elif callable(self.logger):
            self.logger(message)

    # ------------------------------------------------------------ 数据源
    def _http(self, timeout: float, retries: int) -> HttpClient:
        return HttpClient(
            user_agent=self.config.user_agent,
            timeout=timeout,
            max_retries=retries,
            proxy=self.config.proxy,
        )

    def modrinth(self) -> modrinth_source.ModrinthSource:
        if self._modrinth is None:
            self._modrinth = modrinth_source.ModrinthSource(
                client=self._http(self.config.modrinth_timeout, self.config.modrinth_max_retries),
                cache=self.cache,
                api_base=self.config.modrinth_api_base,
                batch_size=self.config.modrinth_batch_size,
                enabled=self.config.use_modrinth,
            )
        return self._modrinth

    def mcmod(self) -> mcmod_source.McModSource:
        if self._mcmod is None:
            self._mcmod = mcmod_source.McModSource(
                client=self._http(self.config.mcmod_timeout, self.config.mcmod_max_retries),
                cache=self.cache,
                base=self.config.mcmod_base,
                min_interval=self.config.mcmod_min_interval,
                enabled=self.config.use_mcmod,
            )
        return self._mcmod

    # ------------------------------------------------------------ 主流程
    def scan(
        self,
        mods_dir: Path,
        progress: Optional[ProgressFn] = None,
        cancel: Optional[CancelFn] = None,
        limit: int = 0,
    ) -> ScanReport:
        """扫描一个 ``mods/`` 目录并给出每个 jar 的侧别判定。

        :param limit: ``>0`` 时只处理前 N 个 jar（冒烟测试 / 演示用）
        """
        started = time.monotonic()
        root = Path(mods_dir)
        report = ScanReport(
            generated_at=now_iso(),
            mods_dir=str(root),
            unknown_as=self.config.unknown_as,
        )

        def emit(phase: str, done: int, total: int, message: str) -> None:
            if progress is not None:
                progress(phase, done, total, message)

        def stopped() -> bool:
            return bool(cancel is not None and cancel())

        if not root.is_dir():
            report.errors.append(f"目录不存在：{root}")
            report.elapsed = time.monotonic() - started
            return report

        jars = iter_jars(root)
        if limit and limit > 0:
            jars = jars[:limit]
        if not jars:
            report.errors.append(f"目录里没有 .jar 文件：{root}")
            report.elapsed = time.monotonic() - started
            return report

        # ---------------------------------------------------------- 1) jar 元数据
        infos: List[JarInfo] = []
        total = len(jars)
        for index, path in enumerate(jars, start=1):
            if stopped():
                report.canceled = True
                break
            rel = str(path.relative_to(root)).replace("\\", "/")
            infos.append(self._inspect(path, rel))
            emit("scan", index, total, f"解析 jar 元数据：{rel}")
            if index % _AUTOSAVE_EVERY == 0:
                self.cache.save()
        if report.canceled:
            report.mods = [merge_verdicts(info, {}, None, self.config.unknown_as) for info in infos]
            report.elapsed = time.monotonic() - started
            self.cache.save()  # 取消也要把已抓到的结果保住
            return report

        self._log("info", f"扫描到 {len(infos)} 个 jar，开始查询数据源")

        # ---------------------------------------------------------- 2) Modrinth
        verdicts: Dict[str, Dict[str, SideVerdict]] = {info.rel: {} for info in infos}
        modrinth = self.modrinth()
        if self.config.use_modrinth and not stopped():
            pairs = [(info.rel, info.sha1) for info in infos if info.sha1]
            hits = modrinth.lookup(pairs, progress=lambda done, total_, msg: emit("modrinth", done, total_, msg))
            for rel, verdict in hits.items():
                verdicts.setdefault(rel, {})["modrinth"] = verdict
            report.errors.extend(modrinth.errors)
            self._log("info", f"Modrinth：{modrinth.resolved_count} 个 jar 找到项目，{modrinth.hit_count} 个得到有效 side 判定")

        # ---------------------------------------------------------- 3) mcmod（串行限流）
        mcmod = self.mcmod()
        if self.config.use_mcmod and not stopped():
            targets = list(infos)
            if str(self.config.mcmod_scope).lower() == "missing":
                # 用户选的加速模式：只补 Modrinth 没给出有效结论的那些。
                # 代价是放弃这部分 mod 的双源交叉验证，所以**默认不开**。
                targets = [
                    info
                    for info in infos
                    if not (verdicts.get(info.rel, {}).get("modrinth") or SideVerdict()).known
                ]
                self._log(
                    "info",
                    "mcmod 查询范围=missing：Modrinth 已判定的 {} 个跳过，只查 {} 个".format(
                        len(infos) - len(targets), len(targets)
                    ),
                )

            def _mcmod_progress(done: int, total_: int, msg: str) -> None:
                emit("mcmod", done, total_, msg)
                # 周期落盘：中途取消不能把已抓到的结果丢掉（实测踩过 —— 取消后缓存归零）
                if done % _AUTOSAVE_EVERY == 0:
                    self.cache.save()

            results = mcmod.lookup_many(
                targets,
                progress=_mcmod_progress,
                should_stop=stopped,
            )
            for rel, verdict in results.items():
                verdicts.setdefault(rel, {})["mcmod"] = verdict
            report.errors.extend(mcmod.errors)
            self._log("info", f"mcmod：{len(results)} 个 jar 得到有效侧别判定")

        # ---------------------------------------------------------- 4) 启发式 + 融合
        mods: List[ModResult] = []
        for index, info in enumerate(infos, start=1):
            if stopped():
                report.canceled = True
                break
            source_verdicts = verdicts.get(info.rel, {})
            if self.config.use_heuristics:
                source_verdicts["heuristics"] = heuristics_source.evaluate(info)
            override = self.cache.get_override(info.sha1, info.primary_mod_id)
            mods.append(merge_verdicts(info, source_verdicts, override, self.config.unknown_as))
            emit("merge", index, len(infos), f"融合判定：{info.rel}")

        report.mods = mods
        report.cache_stats = self.cache.stats()
        report.network_requests = {
            "modrinth": modrinth.requests_made,
            "mcmod": mcmod.requests_made,
        }
        report.source_stats = self._source_stats(mods, modrinth, mcmod)
        report.elapsed = time.monotonic() - started
        self.cache.save()
        return report

    # ------------------------------------------------------------ 单个 jar
    def _inspect(self, path: Path, rel: str) -> JarInfo:
        """读 jar 信息（带缓存，避免重复算哈希 —— 817 MB 的整合包省下十几秒）。"""
        try:
            stat = path.stat()
        except OSError:
            return inspect_jar(path, rel, with_hash=False)
        key = jar_cache_key(rel, stat.st_size, stat.st_mtime_ns)
        if self.config.reuse_jar_hash:
            cached = self.cache.get_jar(key)
            if cached:
                return JarInfo.from_dict(cached, path=path, rel=rel)
        info = inspect_jar(path, rel, with_hash=True)
        payload = info.to_dict()
        payload["cached_ts"] = time.time()
        self.cache.put_jar(key, payload)
        return info

    # ------------------------------------------------------------ 统计
    def _source_stats(
        self,
        mods: Sequence[ModResult],
        modrinth: modrinth_source.ModrinthSource,
        mcmod: mcmod_source.McModSource,
    ) -> Dict[str, Any]:
        total = len(mods)
        stats: Dict[str, Any] = {"total": total}
        for name in ("mcmod", "modrinth", "heuristics", "manual"):
            present = [mod for mod in mods if name in mod.sources]
            known = [
                mod
                for mod in present
                if str(mod.sources[name].get("side") or SIDE_UNKNOWN) != SIDE_UNKNOWN
            ]
            stats[name] = {
                "queried": len(present),
                "hit": len(known),
                "hit_rate": round(len(known) / total, 4) if total else 0.0,
            }
        stats["conflict"] = sum(1 for mod in mods if mod.conflict)
        stats["needs_review"] = sum(1 for mod in mods if mod.needs_review)
        return stats
