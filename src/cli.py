"""命令行入口：``scan`` / ``review`` / ``classify`` / ``cache``。

CLI 先行的理由见 ``PROMPT.md`` 第七节：把准确率跑出来再套 GUI。
GUI（:mod:`src.main`）与 CLI 共用 :class:`~src.detector.Detector`，逻辑只有一份。

用法示例::

    python -m src scan "D:\\mc\\mods"
    python -m src scan "D:\\mc\\mods" --limit 20 --no-mcmod
    python -m src scan "D:\\mc\\mods" --for-autosync "D:\\AutoSync\\data"
    python -m src review "D:\\mc\\mods" --name sodium.jar --side client
    python -m src classify "D:\\mc\\mods" --out "D:\\mc\\server"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import APP_NAME, APP_TITLE, TOOL_ID, __version__
from .cache import CacheManager, now_iso
from .config import Config, load_config, save_config
from .detector import Detector, ScanReport
from .jarinfo import inspect_jar, iter_jars
from .paths import default_config_path, resolve_cache_dir, resolve_output_dir
from .report import (
    JSON_NAME,
    autosync_payload,
    classify_copy,
    write_csv,
    write_json,
    write_txt,
)
from .verdict import SIDE_LABELS, SIDES, normalize_side, side_label

__all__ = ["main", "build_parser"]


# ---------------------------------------------------------------- 输出
class Console:
    """极简控制台输出：进度行原位刷新，正式信息一行一条。"""

    def __init__(self, quiet: bool = False, verbose: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self._interactive = sys.stderr.isatty()
        self._last_len = 0

    def info(self, message: str) -> None:
        if self.quiet:
            return
        self._clear_progress()
        print(message)

    def debug(self, message: str) -> None:
        if self.quiet or not self.verbose:
            return
        self._clear_progress()
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        if self.quiet:
            return
        self._clear_progress()
        print(message, file=sys.stderr)

    def progress(self, phase: str, done: int, total: int, message: str) -> None:
        if self.quiet or not self._interactive:
            return
        total = max(1, total)
        ratio = min(1.0, done / total)
        bar_width = 24
        filled = int(bar_width * ratio)
        bar = "#" * filled + "-" * (bar_width - filled)
        line = f"\r[{phase:<8}] [{bar}] {done}/{total} {message[:48]:<48}"
        padding = max(0, self._last_len - len(line))
        sys.stderr.write(line + " " * padding)
        sys.stderr.flush()
        self._last_len = len(line)

    def _clear_progress(self) -> None:
        if self._last_len:
            sys.stderr.write("\r" + " " * self._last_len + "\r")
            sys.stderr.flush()
            self._last_len = 0


class _Logger:
    """把 Detector 的日志接到 Console 上。"""

    def __init__(self, console: Console) -> None:
        self.console = console

    def info(self, message: str) -> None:
        self.console.debug(message)

    def warning(self, message: str) -> None:
        self.console.debug(message)

    def error(self, message: str) -> None:
        self.console.warn(message)


# ---------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_TITLE} —— 判断模组是纯客户端 / 纯服务端 / 双端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            f"  {APP_NAME} scan \"D:\\mc\\mods\"\n"
            f"  {APP_NAME} scan \"D:\\mc\\mods\" --limit 20 --no-mcmod\n"
            f"  {APP_NAME} scan \"D:\\mc\\mods\" --for-autosync \"D:\\AutoSync\\data\"\n"
            f"  {APP_NAME} review \"D:\\mc\\mods\" --name sodium.jar --side client\n"
            f"  {APP_NAME} classify \"D:\\mc\\mods\" --out \"D:\\mc\\server\"\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_ID}")
    parser.add_argument("--config", default="", help="配置文件路径（默认 <程序目录>/config.json）")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command")

    # ---- scan ----
    scan = sub.add_parser("scan", help="扫描 mods 目录并导出报告")
    scan.add_argument("mods_dir", help="包含 .jar 的目录")
    scan.add_argument("--out-dir", default="", help="报告输出目录（默认与 mods 目录相同）")
    scan.add_argument("--json", default="", help="只写 JSON 到指定路径")
    scan.add_argument("--csv", default="", help="只写 CSV 到指定路径")
    scan.add_argument("--txt", default="", help="只写 TXT 到指定路径")
    scan.add_argument("--for-autosync", default="", help="额外写一份 AutoSync 可读的 side-report.json 到该目录")
    scan.add_argument("--limit", type=int, default=0, help="只扫描前 N 个 jar（冒烟测试用）")
    scan.add_argument("--no-mcmod", action="store_true", help="禁用 MC 百科数据源")
    scan.add_argument("--no-modrinth", action="store_true", help="禁用 Modrinth 数据源")
    scan.add_argument("--no-heuristics", action="store_true", help="禁用启发式弱信号")
    scan.add_argument("--offline", action="store_true", help="完全离线（等价于禁用 mcmod + Modrinth）")
    scan.add_argument("--refresh", action="store_true", help="忽略缓存重新查询")
    scan.add_argument("--no-hash-cache", action="store_true", help="不复用 jar 哈希缓存")
    scan.add_argument("--min-interval", type=float, default=None, help="mcmod 抓取间隔秒数（默认 0.6）")
    scan.add_argument("--json-only", action="store_true", help="只写 JSON，不写 CSV/TXT")

    # ---- review ----
    review = sub.add_parser("review", help="人工确认某个 mod 的侧别（写入缓存，永久生效）")
    review.add_argument("mods_dir", help="包含 .jar 的目录")
    review.add_argument(
        "--name",
        required=True,
        help="jar 文件名；不必写全，唯一匹配即可（实际文件名常带 [中文名] 前缀）",
    )
    review.add_argument(
        "--side",
        default="",
        help="人工判定结果：client / server / both / unknown（--clear 时不需要）",
    )
    review.add_argument("--note", default="", help="备注")
    review.add_argument("--clear", action="store_true", help="清除该 mod 的人工确认记录")

    # ---- classify ----
    classify = sub.add_parser("classify", help="按上次扫描结果把 jar 分类复制到三个目录")
    classify.add_argument("mods_dir", help="包含 .jar 的目录")
    classify.add_argument("--out", required=True, help="输出目录（会创建 client-mods / server-mods / both-mods）")
    classify.add_argument("--report", default="", help="side-report.json 路径（默认取 mods 目录下的）")
    classify.add_argument("--overwrite", action="store_true", help="目标同名文件内容不同时覆盖（默认不覆盖）")
    classify.add_argument("--no-mcmod", action="store_true", help=argparse.SUPPRESS)
    classify.add_argument("--no-modrinth", action="store_true", help=argparse.SUPPRESS)

    # ---- cache ----
    cache = sub.add_parser("cache", help="查看 / 清理缓存")
    cache.add_argument("--clear", action="store_true", help="删除缓存文件")
    cache.add_argument("--show", action="store_true", help="显示缓存路径与统计")

    return parser


# ---------------------------------------------------------------- 命令实现
def _load(path: Path, args: argparse.Namespace, console: Console) -> Tuple[Config, Config]:
    """读配置，返回 ``(磁盘上的配置, 叠加命令行开关后的生效配置)``。

    **命令行开关只对本次运行生效，绝不写回 config.json。** 反例（真实踩过）：
    跑一次 ``scan --no-mcmod`` 会把 ``use_mcmod=false`` 持久化，之后每次扫描都
    再也不查 MC 百科 —— 用户看到的是「mcmod 明明开着却一次请求都不发」。
    """
    base = load_config(path)
    config = Config.from_dict(base.to_dict())
    if args.command == "scan" or args.command == "classify":
        if getattr(args, "no_mcmod", False):
            config.use_mcmod = False
        if getattr(args, "no_modrinth", False):
            config.use_modrinth = False
        if getattr(args, "offline", False):
            config.use_mcmod = False
            config.use_modrinth = False
        if getattr(args, "no_heuristics", False):
            config.use_heuristics = False
        if getattr(args, "no_hash_cache", False):
            config.reuse_jar_hash = False
        if getattr(args, "min_interval", None) is not None:
            config.mcmod_min_interval = float(args.min_interval)
    return base, config


def _make_cache(config: Config, path: Path, refresh: bool) -> CacheManager:
    cache_dir = resolve_cache_dir(config.cache_dir)
    manager = CacheManager.load(cache_dir / "side-cache.json", ttl_hours=config.cache_ttl_hours)
    if refresh:
        manager.jars.clear()
        manager.mcmod_env.clear()
        manager.mcmod_cid.clear()
        manager.modrinth.clear()
    return manager


def cmd_scan(
    args: argparse.Namespace, config: Config, console: Console, base_config: Optional[Config] = None
) -> int:
    mods_dir = Path(args.mods_dir).expanduser()
    if not mods_dir.is_dir():
        console.warn(f"[x] 目录不存在：{mods_dir}")
        return 2
    return _run_scan(args, config, console, mods_dir, base_config)


def _run_scan(
    args: argparse.Namespace,
    config: Config,
    console: Console,
    mods_dir: Path,
    base_config: Optional[Config] = None,
) -> int:
    cache = _make_cache(config, mods_dir, bool(args.refresh))
    detector = Detector(config=config, cache=cache, logger=_Logger(console))

    console.info(f"扫描目录：{mods_dir}")
    started = time.monotonic()
    report = detector.scan(mods_dir, progress=console.progress, limit=int(getattr(args, "limit", 0) or 0))
    console.info("")
    console.info(report.summary_text())

    if report.errors:
        console.warn(f"！有 {len(report.errors)} 条查询错误（结果可能不完整）：")
        for item in report.errors[:5]:
            console.warn(f"    {item}")
        if len(report.errors) > 5:
            console.warn(f"    …（其余 {len(report.errors) - 5} 条见报告）")

    # ---------------------------------------------------------- 导出
    written: List[Path] = []
    if args.json or args.csv or args.txt:
        if args.json:
            written.append(write_json(report, Path(args.json)))
        if args.csv:
            written.append(write_csv(report, Path(args.csv)))
        if args.txt:
            written.append(write_txt(report, Path(args.txt)))
    else:
        out_dir = resolve_output_dir(args.out_dir or config.output_dir, mods_dir)
        written.append(write_json(report, out_dir / "side-report.json"))
        if not args.json_only:
            written.append(write_csv(report, out_dir / "side-report.csv"))
            written.append(write_txt(report, out_dir / "side-report.txt"))

    if args.for_autosync:
        target = Path(args.for_autosync)
        path = target / JSON_NAME if target.is_dir() or not target.suffix else target
        written.append(write_json(report, path, payload=autosync_payload(report)))
        console.info(f"AutoSync 联动文件已写出：{path}")

    for path in written:
        console.info(f"已写出：{path}")

    console.info(f"总耗时：{time.monotonic() - started:.1f} 秒")

    # ---------------------------------------------------------- 高亮清单
    conflicts = [mod for mod in report.mods if mod.conflict]
    if conflicts:
        console.info("")
        console.info(f"【两源冲突 {len(conflicts)} 项 —— 最需要人工确认】")
        for mod in conflicts[:20]:
            console.info(f"  [!] {mod.name}｜{mod.source_summary}｜当前判定 {mod.side_label}")

    reviews = [mod for mod in report.mods if mod.needs_review and not mod.conflict]
    if reviews:
        console.info("")
        console.info(f"【需人工确认 {len(reviews)} 项】")
        for mod in reviews[:20]:
            console.info(f"  [?] {mod.name}｜{mod.side_label}｜{mod.notes[:80]}")
        if len(reviews) > 20:
            console.info(f"  …（其余 {len(reviews) - 20} 项见报告）")

    # 只把「上次扫描目录」这类记忆写回磁盘 —— **命令行开关绝不持久化**
    # （否则一次 `--no-mcmod` 会让之后每次扫描都永远不查 MC 百科）
    target_config = base_config if base_config is not None else config
    target_config.mods_dir = str(mods_dir)
    config_file = Path(getattr(args, "config_path", None) or default_config_path())
    try:
        save_config(config_file, target_config)
    except OSError:
        pass

    return 0 if not report.errors else 0


def _find_mod_file(mods_dir: Path, name: str) -> Tuple[Optional[Path], str]:
    """把 ``--name`` 解析成一个真实文件。

    先按精确路径（相对 ``mods_dir``）找；找不到再按**不完整文件名**做唯一匹配 ——
    实测 mods 目录里的 jar 普遍带 ``[中文名]`` 前缀，要求用户写全名太苛刻。
    模糊匹配到多个时**拒绝执行并列出候选**，绝不猜。
    """
    raw = str(name or "").strip()
    if not raw:
        return None, "文件名为空"
    exact = mods_dir / raw
    if exact.is_file():
        return exact, ""

    needle = raw.lower()
    candidates: List[Path] = []
    try:
        for path in mods_dir.rglob("*"):
            if path.is_file() and needle in path.name.lower():
                candidates.append(path)
    except OSError as exc:
        return None, f"遍历目录失败：{exc!r}"

    if not candidates:
        return None, f"在 {mods_dir} 里找不到匹配「{raw}」的文件"
    if len(candidates) > 1:
        preview = "\n      ".join(sorted(path.name for path in candidates)[:10])
        more = f"\n      …（共 {len(candidates)} 个）" if len(candidates) > 10 else ""
        return None, f"「{raw}」匹配到 {len(candidates)} 个文件，请写得更具体：\n      {preview}{more}"
    return candidates[0], ""


def cmd_review(
    args: argparse.Namespace, config: Config, console: Console, base_config: Optional[Config] = None
) -> int:
    mods_dir = Path(args.mods_dir).expanduser()
    if not mods_dir.is_dir():
        console.warn(f"[x] 目录不存在：{mods_dir}")
        return 2

    side_arg = str(getattr(args, "side", "") or "").strip().lower()
    if not args.clear:
        if not side_arg:
            console.warn("[x] 需要 --side（client / server / both / unknown）；若想清除记录请改用 --clear")
            return 2
        if side_arg not in ("client", "server", "both", "unknown"):
            console.warn(f"[x] --side 取值无效：{side_arg}（只能是 client / server / both / unknown）")
            return 2

    target, error = _find_mod_file(mods_dir, args.name)
    if target is None:
        console.warn(f"[x] {error}")
        return 2

    rel = str(target.relative_to(mods_dir)).replace("\\", "/")
    cache = _make_cache(config, mods_dir, False)
    info = inspect_jar(target, rel=rel, with_hash=True)
    if args.clear:
        cache.drop_override(info.sha1, info.primary_mod_id)
        cache.save()
        console.info(f"已清除人工确认：{info.name}（modId={info.primary_mod_id or '?'}）")
        return 0

    side = normalize_side(side_arg)
    cache.put_override(side, sha1=info.sha1, mod_id=info.primary_mod_id, note=args.note)
    cache.save()
    console.info(
        f"已记录人工确认：{info.name} -> {side_label(side)}"
        f"（modId={info.primary_mod_id or '?'}，sha1={info.sha1[:12]}…）"
    )
    return 0


def cmd_classify(
    args: argparse.Namespace, config: Config, console: Console, base_config: Optional[Config] = None
) -> int:
    import json

    mods_dir = Path(args.mods_dir).expanduser()
    report_path = Path(args.report) if args.report else (mods_dir / JSON_NAME)
    if not report_path.is_file():
        console.warn(f"[x] 找不到报告：{report_path}（请先运行 scan）")
        return 2

    from .report import ScanReport  # noqa: PLC0415  避免循环导入

    report = _load_report(report_path)
    if report is None:
        console.warn(f"[x] 报告解析失败：{report_path}")
        return 2

    out_dir = Path(args.out).expanduser()
    result = classify_copy(report, mods_dir, out_dir, overwrite=bool(args.overwrite))
    console.info(result.summary_text())
    for item in result.conflicts[:20]:
        console.warn(f"  [冲突] {item}")
    for item in result.errors[:20]:
        console.warn(f"  [错误] {item}")
    console.info(f"输出目录：{out_dir}")
    return 0


def _load_report(path: Path) -> Optional[ScanReport]:
    """从 ``side-report.json`` 还原一个 :class:`ScanReport`（只取分类需要的字段）。"""
    import json

    from .detector import ModResult  # noqa: PLC0415

    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    report = ScanReport()
    report.generated_at = str(data.get("generated") or "")
    report.tool = str(data.get("tool") or "")
    report.mods_dir = str(data.get("mods_dir") or "")
    mods: List[ModResult] = []
    for item in data.get("mods") or []:
        if not isinstance(item, dict):
            continue
        mod = ModResult(
            rel=str(item.get("rel_path") or item.get("file") or ""),
            name=str(item.get("file") or ""),
            size=int(item.get("size") or 0),
            sha1=str(item.get("sha1") or ""),
            sha256=str(item.get("sha256") or ""),
            mod_id=str(item.get("mod_id") or ""),
            display_name=str(item.get("display_name") or ""),
            version=str(item.get("version") or ""),
            side=normalize_side(item.get("side")),
            confidence=str(item.get("confidence") or ""),
            conflict=bool(item.get("conflict")),
            needs_review=bool(item.get("needs_review")),
            manual=bool(item.get("manual")),
            notes=str(item.get("notes") or ""),
            sources={str(k): dict(v) for k, v in (item.get("sources") or {}).items() if isinstance(v, dict)},
        )
        mods.append(mod)
    report.mods = mods
    return report


def cmd_cache(
    args: argparse.Namespace, config: Config, console: Console, base_config: Optional[Config] = None
) -> int:
    cache_dir = resolve_cache_dir(config.cache_dir)
    path = cache_dir / "side-cache.json"
    if args.clear:
        try:
            if path.exists():
                path.unlink()
            console.info(f"已删除缓存：{path}")
        except OSError as exc:
            console.warn(f"[x] 删除失败：{exc!r}")
            return 2
        return 0

    manager = CacheManager.load(path, ttl_hours=config.cache_ttl_hours)
    console.info(f"缓存文件：{path}（存在={path.is_file()}）")
    for key, value in manager.stats().items():
        console.info(f"  {key:<12}{value}")
    console.info(f"TTL：{config.cache_ttl_hours} 小时（<=0 表示不过期）")
    return 0


# ---------------------------------------------------------------- 入口
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(quiet=bool(args.quiet), verbose=bool(args.verbose))

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    args.config_path = config_path
    base_config, config = _load(config_path, args, console)

    handlers = {
        "scan": cmd_scan,
        "review": cmd_review,
        "classify": cmd_classify,
        "cache": cmd_cache,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args, config, console, base_config)
    except KeyboardInterrupt:
        console.warn("\n已中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
