"""CustomTkinter 主界面（``python -m src.main`` 启动）。

线程模型（Tkinter 不是线程安全的，这条不能含糊）::

    主线程                                工作线程
    ──────────────────────────────        ─────────────────────────────
    建控件 / 刷表格 / 弹窗                 Detector.scan(...) / 一键分类复制
        ↑                                     │
        │  after(POLL_MS) 轮询 queue           │ progress / 完成 / 异常
        └──────────── queue.Queue ────────────┘

**子线程里绝不碰任何控件** —— 包括 ``label.configure(...)`` 这种"看起来无害"的
调用。所有跨线程信息一律通过 :class:`queue.Queue` 回主线程处理。

设计约束（来自 ``PROMPT.md`` 2.2 与 ``HANDOFF.md``）：

* mcmod 抓取限流 0.6 秒/请求且必须串行 —— 由 :mod:`src.detector` 保证，GUI 只负责
  把它丢到后台线程，绝不阻塞界面、绝不并发；
* 一键分类**默认只复制不移动**，且有明确的二次确认；
* 冲突项与 unknown 项在表格里高亮，这两类是用户最需要看的；
* 人工修正写进缓存（``sha1`` 精确 + ``modId`` 跨版本兜底），下次扫描直接生效。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from .. import APP_TITLE
from ..cache import CacheManager
from ..config import Config, load_config, save_config
from ..detector import Detector, ModResult, ScanReport
from ..paths import default_config_path, resolve_cache_dir, resolve_output_dir
from ..report import JSON_NAME, autosync_payload, classify_copy, write_all, write_json
from ..upload import UploadResult, send_report_file, send_side_report
from ..verdict import (
    CONFIDENCE_LABELS,
    SIDE_BOTH,
    SIDE_CLIENT,
    SIDE_LABELS,
    SIDE_SERVER,
    SIDE_UNKNOWN,
    normalize_side,
    side_label,
)

__all__ = ["MainWindow", "run", "dnd_available"]

# ---------------------------------------------------------------- 软依赖：拖拽
# tkinterdnd2 是**可选**依赖：装上就启用拖拽，装不上（或 tkdnd 二进制缺失）就
# 静默忽略，界面其余功能完全不受影响。请在 README 里向用户说明这一点。
try:  # pragma: no cover - 取决于运行环境
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_IMPORTED = True
except Exception:  # pragma: no cover
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]
    _DND_IMPORTED = False


def dnd_available() -> bool:
    """拖拽依赖是否**可导入**（不代表 tkdnd 二进制一定加载成功）。"""
    return _DND_IMPORTED


# ---------------------------------------------------------------- 常量
POLL_MS = 60
"""主线程轮询事件队列的间隔（毫秒）。"""

WINDOW_TITLE = APP_TITLE
WINDOW_SIZE = "1320x820"

#: 各阶段在总进度里的权重 —— 用来估算总体进度与剩余时间
PHASE_WEIGHTS: Dict[str, float] = {
    "scan": 0.15,
    "modrinth": 0.20,
    "mcmod": 0.55,
    "merge": 0.10,
}
PHASE_ORDER: Tuple[str, ...] = ("scan", "modrinth", "mcmod", "merge")
PHASE_LABELS: Dict[str, str] = {
    "scan": "解析 jar 元数据",
    "modrinth": "查询 Modrinth",
    "mcmod": "查询 MC 百科（0.6 秒/次，串行）",
    "merge": "融合判定",
}

#: 侧别筛选下拉的可选值
FILTER_ALL = "全部"
FILTER_CLIENT = "纯客户端"
FILTER_SERVER = "纯服务端"
FILTER_BOTH = "双端"
FILTER_UNKNOWN = "不确定"
FILTER_CONFLICT = "仅冲突"
FILTER_REVIEW = "仅需人工确认"
FILTER_VALUES: Tuple[str, ...] = (
    FILTER_ALL,
    FILTER_CLIENT,
    FILTER_SERVER,
    FILTER_BOTH,
    FILTER_UNKNOWN,
    FILTER_CONFLICT,
    FILTER_REVIEW,
)

#: 外观下拉：显示文字 -> Config.appearance 取值
APPEARANCE_LABELS: Dict[str, str] = {
    "深色": "dark",
    "浅色": "light",
    "跟随系统": "system",
}
APPEARANCE_REVERSE: Dict[str, str] = {value: key for key, value in APPEARANCE_LABELS.items()}

#: 表格列：(列 id, 表头文字, 初始宽度, 对齐)
COLUMNS: Tuple[Tuple[str, str, int, str], ...] = (
    ("name", "文件名", 300, "w"),
    ("mod_id", "modId", 160, "w"),
    ("display_name", "显示名", 210, "w"),
    ("side", "判定侧别", 96, "center"),
    ("confidence", "置信度", 76, "center"),
    ("sources", "数据来源", 280, "w"),
    ("notes", "备注", 420, "w"),
)

#: 置信度排序权重（高 -> 低）
CONF_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}
#: 侧别排序权重：越"危险"（纯服务端）越靠前
SIDE_ORDER: Dict[str, int] = {SIDE_SERVER: 0, SIDE_CLIENT: 1, SIDE_BOTH: 2, SIDE_UNKNOWN: 3}


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化成人话。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {rest:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _sort_key(column: str) -> Callable[[ModResult], Any]:
    """按列返回排序键；字符串统一小写，避免大小写混排。"""
    if column == "name":
        return lambda mod: (mod.name or "").lower()
    if column == "mod_id":
        return lambda mod: (mod.mod_id or "").lower()
    if column == "display_name":
        return lambda mod: (mod.display_name or "").lower()
    if column == "side":
        return lambda mod: SIDE_ORDER.get(mod.side, 9)
    if column == "confidence":
        return lambda mod: CONF_ORDER.get(mod.confidence, 9)
    if column == "sources":
        return lambda mod: (mod.source_summary or "").lower()
    return lambda mod: (mod.notes or "").lower()


class _QueueLogger:
    """把 :class:`~src.detector.Detector` 的日志转成队列事件（不直接碰控件）。"""

    def __init__(self, events: "queue.Queue[Tuple[str, Any]]") -> None:
        self.events = events

    def info(self, message: str) -> None:
        self.events.put(("log", str(message)))

    # Detector 可能调用 warning / error，这里统一对待
    warning = info
    error = info
    debug = info


class MainWindow(ctk.CTk):
    """主窗口。所有控件操作都发生在主线程。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        mods_dir: Optional[str] = None,
        auto_scan: bool = False,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        # ---- 先读配置：外观模式必须在建控件之前设定 ----
        self.config_path: Path = Path(config_path) if config_path else default_config_path()
        self.config: Config = load_config(self.config_path)
        if mods_dir:
            self.config.mods_dir = str(mods_dir)
        self._on_close_hook = on_close

        ctk.set_appearance_mode(self._appearance_mode(self.config.appearance))
        try:
            ctk.set_default_color_theme("blue")
        except Exception:  # pragma: no cover - 主题文件异常时不该拦住启动
            pass

        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(1000, 640)

        # ---- 运行时状态 ----
        self.report: Optional[ScanReport] = None
        self.cache: Optional[CacheManager] = None
        self._events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._scanning = False
        self._classifying = False
        self._uploading = False
        self._token_visible = False
        self._autosync_report_path: Optional[Path] = None
        self._started_at = 0.0
        self._row_mod: Dict[str, ModResult] = {}
        self._sort_col = "side"
        self._sort_desc = False
        self._dnd_ok = False
        self._last_progress = 0.0

        self._build_ui()
        if self.config.mods_dir:
            self.path_var.set(self.config.mods_dir)

        self._enable_drag_and_drop()
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        if auto_scan:
            # 让窗口先画出来，再开始扫描（否则启动瞬间是白屏）
            self.after(200, self.start_scan)

    # ================================================================ 界面搭建
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # 表格区域吃掉多余空间

        self._build_toolbar(row=0)
        self._build_progress(row=1)
        self._build_filter(row=2)
        self._build_table(row=3)
        self._build_actions(row=4)
        self._build_statusbar(row=5)

    # ---------------------------------------------------------------- 顶部工具条
    def _build_toolbar(self, row: int) -> None:
        bar = ctk.CTkFrame(self)
        bar.grid(row=row, column=0, sticky="ew", padx=10, pady=(10, 4))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bar, text="mods 目录：", font=ctk.CTkFont(size=13)).grid(
            row=0, column=0, padx=(10, 4), pady=10, sticky="w"
        )

        self.path_var = tk.StringVar(value="")
        self.path_entry = ctk.CTkEntry(
            bar, textvariable=self.path_var, placeholder_text="选择或拖入包含 .jar 的文件夹…"
        )
        self.path_entry.grid(row=0, column=1, padx=4, pady=10, sticky="ew")

        self.browse_button = ctk.CTkButton(bar, text="选文件夹", width=96, command=self.choose_folder)
        self.browse_button.grid(row=0, column=2, padx=4, pady=10)

        self.scan_button = ctk.CTkButton(bar, text="开始扫描", width=96, command=self.start_scan)
        self.scan_button.grid(row=0, column=3, padx=4, pady=10)

        self.cancel_button = ctk.CTkButton(
            bar, text="取消", width=72, command=self.cancel_scan, state="disabled", fg_color="#8a3b3b"
        )
        self.cancel_button.grid(row=0, column=4, padx=(4, 10), pady=10)

        # ---- 第二行：数据源开关 + 外观 ----
        self.mcmod_var = tk.BooleanVar(value=bool(self.config.use_mcmod))
        self.modrinth_var = tk.BooleanVar(value=bool(self.config.use_modrinth))
        self.heuristics_var = tk.BooleanVar(value=bool(self.config.use_heuristics))

        ctk.CTkLabel(bar, text="数据源：").grid(row=1, column=0, padx=(10, 4), pady=(0, 10), sticky="w")
        switches = ctk.CTkFrame(bar, fg_color="transparent")
        switches.grid(row=1, column=1, columnspan=3, padx=0, pady=(0, 10), sticky="w")

        self.source_switches: List[ctk.CTkSwitch] = []
        for text, var in (
            ("MC 百科(mcmod)", self.mcmod_var),
            ("Modrinth", self.modrinth_var),
            ("启发式弱信号", self.heuristics_var),
        ):
            switch = ctk.CTkSwitch(switches, text=text, variable=var, command=self._on_toggle_sources)
            switch.pack(side="left", padx=(0, 16))
            self.source_switches.append(switch)

        # mcmod 是串行限流（0.6 秒/请求），全量交叉验证要几分钟。
        # 打开这个开关就只补 Modrinth 没给出结论的那些，用来换速度。
        self.mcmod_scope_var = tk.BooleanVar(value=str(self.config.mcmod_scope) == "missing")
        scope_switch = ctk.CTkSwitch(
            switches,
            text="mcmod 只补缺失（快）",
            variable=self.mcmod_scope_var,
            command=self._on_toggle_sources,
        )
        scope_switch.pack(side="left", padx=(0, 16))
        self.source_switches.append(scope_switch)

        appearance_box = ctk.CTkFrame(bar, fg_color="transparent")
        appearance_box.grid(row=1, column=4, padx=(4, 10), pady=(0, 10), sticky="e")
        ctk.CTkLabel(appearance_box, text="外观：").pack(side="left", padx=(0, 6))
        self.appearance_menu = ctk.CTkOptionMenu(
            appearance_box,
            width=110,
            values=list(APPEARANCE_LABELS.keys()),
            command=self._on_appearance_change,
        )
        self.appearance_menu.set(APPEARANCE_REVERSE.get(self.config.appearance, "深色"))
        self.appearance_menu.pack(side="left")

        hint = "可把 mods 文件夹直接拖到此处" if dnd_available() else "（未安装 tkinterdnd2，拖拽功能已禁用）"
        self.dnd_hint = ctk.CTkLabel(bar, text=hint, text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11))
        self.dnd_hint.grid(row=2, column=0, columnspan=5, padx=10, pady=(0, 8), sticky="w")

    # ---------------------------------------------------------------- 进度区
    def _build_progress(self, row: int) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=row, column=0, sticky="ew", padx=10, pady=4)
        frame.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(frame)
        self.progress.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self.progress.set(0)

        info = ctk.CTkFrame(frame, fg_color="transparent")
        info.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        info.grid_columnconfigure(1, weight=1)

        self.phase_label = ctk.CTkLabel(info, text="就绪", width=220, anchor="w")
        self.phase_label.grid(row=0, column=0, sticky="w")
        self.file_label = ctk.CTkLabel(info, text="", anchor="w", text_color=("gray30", "gray70"))
        self.file_label.grid(row=0, column=1, sticky="ew", padx=8)
        self.time_label = ctk.CTkLabel(info, text="", anchor="e", width=260)
        self.time_label.grid(row=0, column=2, sticky="e")

    # ---------------------------------------------------------------- 筛选区
    def _build_filter(self, row: int) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=row, column=0, sticky="ew", padx=10, pady=4)
        frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(frame, text="筛选：").grid(row=0, column=0, padx=(10, 4), pady=8, sticky="w")
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        self.filter_menu = ctk.CTkOptionMenu(
            frame, width=140, values=list(FILTER_VALUES), variable=self.filter_var, command=lambda _v: self._refresh_table()
        )
        self.filter_menu.grid(row=0, column=1, padx=4, pady=8, sticky="w")

        ctk.CTkLabel(frame, text="搜索：").grid(row=0, column=2, padx=(16, 4), pady=8, sticky="w")
        self.search_var = tk.StringVar(value="")
        search_entry = ctk.CTkEntry(frame, textvariable=self.search_var, placeholder_text="按文件名 / modId / 显示名过滤")
        search_entry.grid(row=0, column=3, padx=4, pady=8, sticky="ew")
        self.search_var.trace_add("write", lambda *_a: self._refresh_table())

        self.count_label = ctk.CTkLabel(frame, text="0 / 0", anchor="e", width=140)
        self.count_label.grid(row=0, column=4, padx=(8, 10), pady=8, sticky="e")

    # ---------------------------------------------------------------- 结果表格
    def _build_table(self, row: int) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=row, column=0, sticky="nsew", padx=10, pady=4)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        holder = tk.Frame(frame, bd=0, highlightthickness=0)
        holder.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            holder,
            columns=[col[0] for col in COLUMNS],
            show="headings",
            selectmode="browse",
        )
        for col_id, title, width, anchor in COLUMNS:
            self.tree.heading(col_id, text=title, command=lambda c=col_id: self._sort_by(c))
            self.tree.column(col_id, width=width, anchor=anchor, stretch=(col_id in ("notes", "name")))

        y_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(holder, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        # 双击某行 -> 人工修正
        self.tree.bind("<Double-1>", self._on_row_double_click)
        # 键盘操作也能改（回车）
        self.tree.bind("<Return>", lambda _e: self._edit_selected_row())

        self._apply_tree_style()
        self._sync_sort_heading()

    def _apply_tree_style(self) -> None:
        """给 ttk.Treeview 套上跟当前外观一致的颜色（ctk 没有表格控件）。"""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")  # clam 才能可靠地自定义配色
        except tk.TclError:  # pragma: no cover
            pass

        dark = self._is_dark()
        if dark:
            background, field, foreground = "#1b1b1b", "#242424", "#e8e8e8"
            heading_bg, heading_fg = "#2f2f2f", "#e8e8e8"
            even_bg, odd_bg = "#242424", "#2a2a2a"
            sel_bg, sel_fg = "#1f6aa5", "#ffffff"
            tags = {
                # 冲突：最刺眼的红 —— 两源打架，必须人工看
                "conflict": {"background": "#7d2222", "foreground": "#ffe9e9"},
                # 不确定：琥珀色 —— 没有数据源敢下结论
                "unknown": {"background": "#6b4e00", "foreground": "#fff3cf"},
                # 需人工确认（非冲突）：偏橄榄
                "review": {"background": "#4a4a22", "foreground": "#f3f3d0"},
                # 已人工确认：绿色
                "manual": {"background": "#14532d", "foreground": "#d8f7e3"},
            }
        else:
            background, field, foreground = "#ffffff", "#fbfbfb", "#1a1a1a"
            heading_bg, heading_fg = "#e6e6e6", "#1a1a1a"
            even_bg, odd_bg = "#ffffff", "#f2f2f2"
            sel_bg, sel_fg = "#3b8ed0", "#ffffff"
            tags = {
                "conflict": {"background": "#ffd6d6", "foreground": "#7a1010"},
                "unknown": {"background": "#ffeab8", "foreground": "#6b4e00"},
                "review": {"background": "#f0f0cf", "foreground": "#4a4a22"},
                "manual": {"background": "#d6f5e0", "foreground": "#0f5132"},
            }

        style.configure(
            "Treeview",
            background=field,
            fieldbackground=field,
            foreground=foreground,
            rowheight=26,
            font=("Microsoft YaHei UI", 10),
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", sel_bg)],
            foreground=[("selected", sel_fg)],
        )
        style.configure(
            "Treeview.Heading",
            background=heading_bg,
            foreground=heading_fg,
            font=("Microsoft YaHei UI", 10, "bold"),
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", sel_bg)])
        # 外框底色：让 Treeview 与 CustomTkinter 的卡片融在一起
        style.configure("Treeview", bordercolor=background)

        self.tree.tag_configure("even", background=even_bg)
        self.tree.tag_configure("odd", background=odd_bg)
        for name, options in tags.items():
            self.tree.tag_configure(name, **options)

    # ---------------------------------------------------------------- 底部操作
    def _build_actions(self, row: int) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=row, column=0, sticky="ew", padx=10, pady=4)

        # 第一行：上报到 AutoSync 的连接设置
        self._build_autosync_row(frame)

        # 导出目录（常驻设置）：留空 = 导出到扫描目录（mods），与 AutoSync 默认探测位置一致
        out_row = ctk.CTkFrame(frame, fg_color="transparent")
        out_row.pack(fill="x", padx=10, pady=(8, 0))
        out_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(out_row, text="导出目录：").grid(row=0, column=0, padx=(0, 4), sticky="w")
        self.out_dir_var = tk.StringVar(value=str(self.config.output_dir or ""))
        self.out_dir_entry = ctk.CTkEntry(
            out_row,
            textvariable=self.out_dir_var,
            placeholder_text="留空 = 导出到扫描目录（mods/，AutoSync 默认去这里找）",
        )
        self.out_dir_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ctk.CTkButton(out_row, text="浏览…", width=76, command=self._pick_output_dir).grid(
            row=0, column=2, padx=(0, 6)
        )
        ctk.CTkButton(out_row, text="恢复默认", width=86, command=self._reset_output_dir).grid(
            row=0, column=3, padx=(0, 10)
        )

        # 第二行：提示 + 导出类按钮
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.pack(fill="x")

        ctk.CTkLabel(
            bar,
            text="提示：双击任意一行可人工修正判定（写入缓存，下次扫描直接沿用）",
            text_color=("gray35", "gray65"),
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=10, pady=8)

        self.classify_button = ctk.CTkButton(
            bar, text="一键分类（复制）…", width=150, command=self.classify_copy_action
        )
        self.classify_button.pack(side="right", padx=(6, 10), pady=8)
        self.autosync_button = ctk.CTkButton(
            bar, text="导出到 AutoSync 目录…", width=170, command=self.export_to_autosync
        )
        self.autosync_button.pack(side="right", padx=6, pady=8)
        self.export_button = ctk.CTkButton(bar, text="导出报告…", width=110, command=self.export_reports)
        self.export_button.pack(side="right", padx=6, pady=8)

    def _build_autosync_row(self, parent: Any) -> None:
        """「上报到 AutoSync」设置条：开关 / 地址 / 端口 / 令牌 / 立即上报 / 保存设置。

        放在底部操作区（表格下方、状态栏上方），不挤占数据源开关那一排。
        令牌默认遮罩显示，旁边的小按钮可以临时看明文。
        """
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=10, pady=(8, 0))
        frame.grid_columnconfigure(6, weight=1)  # 令牌输入框吃掉多余宽度

        self.autosync_var = tk.BooleanVar(value=bool(self.config.autosync_report_enabled))
        self.autosync_switch = ctk.CTkSwitch(
            frame, text="上报到 AutoSync", variable=self.autosync_var, command=self._on_toggle_autosync
        )
        self.autosync_switch.grid(row=0, column=0, padx=(0, 12), sticky="w")

        ctk.CTkLabel(frame, text="地址：").grid(row=0, column=1, padx=(0, 4), sticky="w")
        self.autosync_host_var = tk.StringVar(value=self.config.autosync_host)
        self.autosync_host_entry = ctk.CTkEntry(
            frame, textvariable=self.autosync_host_var, width=200, placeholder_text="域名或 IP"
        )
        self.autosync_host_entry.grid(row=0, column=2, padx=(0, 12), sticky="w")

        ctk.CTkLabel(frame, text="端口：").grid(row=0, column=3, padx=(0, 4), sticky="w")
        self.autosync_port_var = tk.StringVar(value=str(self.config.autosync_port))
        self.autosync_port_entry = ctk.CTkEntry(frame, textvariable=self.autosync_port_var, width=72)
        self.autosync_port_entry.grid(row=0, column=4, padx=(0, 12), sticky="w")

        ctk.CTkLabel(frame, text="令牌：").grid(row=0, column=5, padx=(0, 4), sticky="w")
        self.autosync_token_var = tk.StringVar(value=self.config.autosync_token)
        self.autosync_token_entry = ctk.CTkEntry(
            frame, textvariable=self.autosync_token_var, show="*", placeholder_text="共享令牌（token）"
        )
        self.autosync_token_entry.grid(row=0, column=6, padx=(0, 4), sticky="ew")

        self.token_visible_button = ctk.CTkButton(
            frame, text="显示", width=48, command=self._toggle_token_visibility
        )
        self.token_visible_button.grid(row=0, column=7, padx=(0, 12), sticky="w")

        self.report_now_button = ctk.CTkButton(frame, text="立即上报", width=90, command=self.report_now)
        self.report_now_button.grid(row=0, column=8, padx=(0, 6), sticky="w")

        self.save_autosync_button = ctk.CTkButton(
            frame, text="保存设置", width=90, command=self.save_autosync_settings
        )
        self.save_autosync_button.grid(row=0, column=9, sticky="w")

        ctk.CTkLabel(
            frame,
            text=(
                "开关默认关闭；地址或令牌为空时不会发起请求。"
                "「保存设置」写回 config.json（命令行参数不写回）。"
            ),
            text_color=("gray35", "gray65"),
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, columnspan=10, sticky="w", pady=(4, 0))

    # ---------------------------------------------------------------- 状态栏
    def _build_statusbar(self, row: int) -> None:
        bar = ctk.CTkFrame(self, height=28, corner_radius=0)
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(
            bar, text="就绪。选择 mods 目录后点「开始扫描」。", anchor="w", font=ctk.CTkFont(size=11)
        )
        self.status_label.grid(row=0, column=0, sticky="ew", padx=10, pady=4)

    # ================================================================ 外观
    @staticmethod
    def _appearance_mode(value: str) -> str:
        mode = str(value or "").strip().lower()
        return mode if mode in ("dark", "light", "system") else "dark"

    @staticmethod
    def _is_dark() -> bool:
        try:
            return str(ctk.get_appearance_mode()).strip().lower().startswith("dark")
        except Exception:  # pragma: no cover
            return True

    def _on_appearance_change(self, label: str) -> None:
        mode = APPEARANCE_LABELS.get(label, "dark")
        self.config.appearance = mode
        ctk.set_appearance_mode(mode)
        self._apply_tree_style()
        self._refresh_table()
        self._save_config()

    def _on_toggle_sources(self) -> None:
        self.config.use_mcmod = bool(self.mcmod_var.get())
        self.config.use_modrinth = bool(self.modrinth_var.get())
        self.config.use_heuristics = bool(self.heuristics_var.get())
        self.config.mcmod_scope = "missing" if self.mcmod_scope_var.get() else "all"
        self._save_config()

    def _save_config(self) -> None:
        try:
            save_config(self.config_path, self.config)
        except OSError:
            # 配置写不了不该影响使用（例如 exe 放在只读目录）
            pass

    # ================================================================ AutoSync 上报
    def _collect_autosync_settings(self, save: bool = False) -> bool:
        """把界面上的上报设置同步进 ``self.config``；``save=True`` 时写回 config.json。

        端口填得不对时返回 False（并弹窗说明），调用方据此跳过上报。
        """
        self.config.autosync_report_enabled = bool(self.autosync_var.get())
        self.config.autosync_host = self.autosync_host_var.get().strip()
        raw_port = self.autosync_port_var.get().strip()
        try:
            port = int(raw_port)
        except ValueError:
            messagebox.showerror("端口不合法", f"端口必须是数字，当前填的是：{raw_port!r}")
            return False
        if not (0 < port < 65536):
            messagebox.showerror("端口不合法", f"端口应在 1-65535 之间，当前填的是：{port}")
            return False
        self.config.autosync_port = port
        self.config.autosync_token = self.autosync_token_var.get().strip()
        if save:
            self._save_config()
        return True

    def _on_toggle_autosync(self) -> None:
        """开关：与其它开关一致立即落盘（地址 / 令牌仍由「保存设置」负责）。"""
        self.config.autosync_report_enabled = bool(self.autosync_var.get())
        self._save_config()
        state = "已开启" if self.config.autosync_report_enabled else "已关闭"
        self.status_label.configure(text=f"AutoSync 上报{state}（地址 / 令牌改完请点「保存设置」）")

    def _toggle_token_visibility(self) -> None:
        """令牌「显示 / 隐藏」——只影响显示，不改变配置。"""
        self._token_visible = not self._token_visible
        self.autosync_token_entry.configure(show="" if self._token_visible else "*")
        self.token_visible_button.configure(text="隐藏" if self._token_visible else "显示")

    def save_autosync_settings(self) -> None:
        """「保存设置」：把地址 / 端口 / 令牌 / 开关写回 config.json。"""
        if not self._collect_autosync_settings(save=True):
            return
        target = f"{self.config.autosync_host or '（未填地址）'}:{self.config.autosync_port}"
        self.status_label.configure(text=f"AutoSync 上报设置已保存：{target}")
        messagebox.showinfo("已保存", f"上报设置已写入：\n{self.config_path}\n\n目标：{target}")

    def _report_file_path(self) -> Path:
        """上报用文件的落盘位置：上次导出 / 上报过的文件优先，否则用默认输出目录。"""
        if self._autosync_report_path is not None:
            return self._autosync_report_path
        return Path(self._default_out_dir()) / JSON_NAME

    def report_now(self) -> None:
        """「立即上报」：把当前扫描结果落盘成 ``side-report.json`` 后发给 AutoSync。"""
        if self._uploading:
            return
        if self.report is None or not self.report.mods:
            messagebox.showinfo("暂无结果", "请先完成一次扫描，再点「立即上报」。")
            return
        if not self._collect_autosync_settings(save=False):
            return

        path = self._report_file_path()
        try:
            # 先把「即将发出去的内容」落盘：排查问题时能直接看到发的是什么
            write_json(self.report, path, payload=autosync_payload(self.report))
        except OSError as exc:
            messagebox.showerror("写入失败", f"无法写出上报用的 {JSON_NAME}：\n{exc!r}")
            return
        self._autosync_report_path = path
        self._start_upload(path=path, auto=False)

    def _start_upload(
        self,
        path: Optional[Path] = None,
        report: Optional[ScanReport] = None,
        auto: bool = False,
    ) -> None:
        """把上报丢到后台线程。

        **子线程里绝不碰控件** —— 结果通过 ``queue`` 回主线程（与扫描线程同一套做法）。
        """
        config = self.config
        self._uploading = True
        self.report_now_button.configure(state="disabled")
        target = f"{config.autosync_host or '?'}:{config.autosync_port}"
        self.status_label.configure(text=f"正在上报到 AutoSync（{target}）…")
        self.file_label.configure(text=f"AutoSync：{target}")
        self.after(POLL_MS, self._pump)

        def worker() -> None:
            try:
                if path is not None:
                    result = send_report_file(
                        path,
                        config.autosync_host,
                        config.autosync_port,
                        config.autosync_token,
                        config.autosync_timeout,
                    )
                else:
                    result = send_side_report(
                        report,
                        config.autosync_host,
                        config.autosync_port,
                        config.autosync_token,
                        config.autosync_timeout,
                    )
                self._events.put(("upload_done", (result, bool(auto), "")))
            except Exception:  # pragma: no cover - 兜底，上报模块本身已不抛异常
                self._events.put(("upload_done", (None, bool(auto), traceback.format_exc())))

        threading.Thread(target=worker, name="ModSideDetectorUpload", daemon=True).start()

    def _on_upload_done(self, result: Optional[UploadResult], auto: bool, error: str = "") -> None:
        self._uploading = False
        self.report_now_button.configure(state="normal")

        if result is None:
            detail = (error or "未知错误").strip().splitlines()
            text = detail[-1] if detail else "未知错误"
            self.status_label.configure(text=f"上报失败：{text}")
            logging.getLogger(__name__).error("上报失败：\n%s", error)
            messagebox.showerror("上报失败", text)
            return

        if result.ok:
            self.status_label.configure(text=f"上报成功：对方已接收 {result.count} 条")
            self.file_label.configure(text=result.message)
            if not auto:
                messagebox.showinfo(
                    "上报成功",
                    f"{result.message}\n\n对方响应：{result.raw_response.strip() or '(空)'}",
                )
            return

        # 失败：手动上报弹窗；自动上报也弹 —— 开关是用户自己打开的，静默失败最糟糕
        self.status_label.configure(text=f"上报失败：{result.message}")
        self.file_label.configure(text=f"上报失败：{result.message}")
        messagebox.showwarning("上报失败", f"{result.message}\n\n（不影响本次扫描结果）")

    # ================================================================ 拖拽
    def _enable_drag_and_drop(self) -> None:
        """把 tkdnd 挂到本窗口；失败就安静降级（软依赖）。"""
        if not dnd_available():
            return
        try:
            TkinterDnD._require(self)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - 取决于本机 tkdnd 二进制
            logging.getLogger(__name__).warning("拖拽不可用（tkdnd 加载失败）：%s", exc)
            self.dnd_hint.configure(text="（tkdnd 加载失败，拖拽功能已禁用）")
            return

        targets = [self, self.path_entry, self.tree, self.dnd_hint]
        ok = False
        for widget in targets:
            try:
                widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
                widget.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
                ok = True
            except Exception:  # pragma: no cover
                continue
        self._dnd_ok = ok
        if not ok:  # pragma: no cover
            self.dnd_hint.configure(text="（拖拽注册失败，请用「选文件夹」按钮）")

    def _on_drop(self, event: Any) -> None:
        """拖入文件夹（或 .jar 文件）后自动填路径。"""
        try:
            raw_items = self.tk.splitlist(event.data)
        except Exception:
            raw_items = [str(getattr(event, "data", ""))]
        for raw in raw_items:
            path = Path(str(raw).strip("{}").strip())
            if path.is_dir():
                self.path_var.set(str(path))
                self._on_path_changed()
                return
            if path.is_file() and path.suffix.lower() == ".jar":
                self.path_var.set(str(path.parent))
                self._on_path_changed()
                return

    # ================================================================ 目录选择
    def choose_folder(self) -> None:
        initial = self.path_var.get().strip() or self.config.mods_dir or str(Path.home())
        chosen = filedialog.askdirectory(title="选择包含 .jar 的 mods 文件夹", initialdir=initial, mustexist=False)
        if not chosen:
            return
        self.path_var.set(str(Path(chosen)))
        self._on_path_changed()

    def _on_path_changed(self) -> None:
        # 记住上次路径（下次启动直接定位到这里）
        self.config.mods_dir = self.path_var.get().strip()
        self._save_config()

    # ================================================================ 扫描
    def start_scan(self) -> None:
        if self._scanning:
            return
        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("未选择目录", "请先选择（或拖入）一个包含 .jar 的 mods 文件夹。")
            return
        mods_dir = Path(raw).expanduser()
        if not mods_dir.is_dir():
            messagebox.showerror("目录不存在", f"找不到目录：\n{mods_dir}")
            return

        # 把界面上的开关同步进 Config，然后落盘（下次启动保持）
        self._on_toggle_sources()
        self.config.mods_dir = str(mods_dir)
        self._save_config()

        self._scanning = True
        self._cancel_event.clear()
        self._started_at = time.monotonic()
        self._last_progress = 0.0
        self.progress.set(0)
        self.phase_label.configure(text=PHASE_LABELS["scan"])
        self.file_label.configure(text="准备中…")
        self.time_label.configure(text="")
        self.scan_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        for button in (self.export_button, self.autosync_button, self.classify_button, self.report_now_button):
            button.configure(state="disabled")
        # 扫描期间锁住数据源开关：Config 是工作线程在读的，
        # 中途改会让同一次扫描的前后行为不一致
        for switch in self.source_switches:
            switch.configure(state="disabled")
        self.status_label.configure(text=f"正在扫描：{mods_dir}")

        config = self.config
        worker = threading.Thread(
            target=self._scan_worker,
            args=(config, mods_dir),
            name="ModSideDetectorScan",
            daemon=True,
        )
        worker.start()
        self.after(POLL_MS, self._pump)

    def _scan_worker(self, config: Config, mods_dir: Path) -> None:
        """后台线程：跑 Detector。**这里绝对不碰任何控件。**"""
        try:
            cache_dir = resolve_cache_dir(config.cache_dir)
            cache = CacheManager.load(cache_dir / "side-cache.json", ttl_hours=config.cache_ttl_hours)
            detector = Detector(config=config, cache=cache, logger=_QueueLogger(self._events))
            report = detector.scan(
                mods_dir,
                progress=lambda phase, done, total, message: self._events.put(
                    ("progress", (phase, done, total, message))
                ),
                cancel=self._cancel_event.is_set,
            )
            self._events.put(("done", (report, cache)))
        except Exception:  # pragma: no cover - 兜底，避免线程静默死掉
            self._events.put(("error", traceback.format_exc()))

    def cancel_scan(self) -> None:
        if not self._scanning:
            return
        self._cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.phase_label.configure(text="正在中止…")
        self.status_label.configure(text="已请求取消，等待当前请求收尾…")

    # ---------------------------------------------------------------- 事件泵
    def _pump(self) -> None:
        """主线程轮询队列 —— 唯一允许更新控件的地方。"""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._on_progress(*payload)
                elif kind == "log":
                    logging.getLogger("ModSideDetector").debug("%s", payload)
                elif kind == "done":
                    report, cache = payload
                    self._on_scan_done(report, cache)
                elif kind == "error":
                    self._on_scan_error(str(payload))
                elif kind == "classify_done":
                    self._on_classify_done(*payload)
                elif kind == "upload_done":
                    self._on_upload_done(*payload)
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - 界面异常不该让轮询停摆
            logging.getLogger(__name__).exception("刷新界面时出错")
        if self._scanning or self._classifying or self._uploading:
            self.after(POLL_MS, self._pump)

    # ---------------------------------------------------------------- 进度
    def _overall_fraction(self, phase: str, fraction: float) -> float:
        """把「当前阶段完成了多少」换算成总体进度（各阶段权重不同）。"""
        if phase not in PHASE_ORDER:
            return fraction
        index = PHASE_ORDER.index(phase)
        done_weight = sum(PHASE_WEIGHTS[name] for name in PHASE_ORDER[:index])
        return min(1.0, done_weight + PHASE_WEIGHTS.get(phase, 0.0) * fraction)

    def _on_progress(self, phase: str, done: int, total: int, message: str) -> None:
        total = max(1, int(total or 1))
        fraction = min(1.0, max(0.0, done / total))
        overall = self._overall_fraction(phase, fraction)
        self._last_progress = overall
        self.progress.set(overall)
        self.phase_label.configure(text=PHASE_LABELS.get(phase, phase))
        self.file_label.configure(text=f"({done}/{total}) {message}")
        elapsed = time.monotonic() - self._started_at
        eta = elapsed * (1.0 - overall) / overall if overall > 0.02 else 0.0
        self.time_label.configure(text=f"已用 {_fmt_duration(elapsed)} ｜ 预计剩余 {_fmt_duration(eta)}")

    # ---------------------------------------------------------------- 扫描完成
    def _on_scan_done(self, report: ScanReport, cache: CacheManager) -> None:
        self._scanning = False
        self.scan_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        for button in (self.export_button, self.autosync_button, self.classify_button, self.report_now_button):
            button.configure(state="normal")
        for switch in self.source_switches:
            switch.configure(state="normal")

        self.report = report
        self.cache = cache

        elapsed = report.elapsed or (time.monotonic() - self._started_at)
        if report.canceled:
            self.phase_label.configure(text="已取消（结果不完整）")
        else:
            self.progress.set(1.0)
            self.phase_label.configure(text="扫描完成")
        self.file_label.configure(text=report.summary_text())
        self.time_label.configure(text=f"总耗时 {_fmt_duration(elapsed)}")

        self._refresh_table()
        self._update_status()

        if report.errors:
            # 明确告知用户：数据源有错，结果可能不完整（例如 mcmod 不可达）
            head = "\n".join(f"· {item}" for item in report.errors[:6])
            more = f"\n…（其余 {len(report.errors) - 6} 条见导出报告）" if len(report.errors) > 6 else ""
            messagebox.showwarning(
                "部分数据源出错",
                f"扫描完成，但有 {len(report.errors)} 条查询错误，结果可能不完整：\n\n{head}{more}\n\n"
                "提示：mcmod 不可达时可先用 Modrinth + 启发式，联网后重新扫描。",
            )

        # 扫描完成后按开关自动上报（开关关闭时什么都不做；上报失败也不影响扫描结果）
        if bool(self.autosync_var.get()) and self._collect_autosync_settings(save=False):
            self._start_upload(report=report, auto=True)

    def _on_scan_error(self, detail: str) -> None:
        self._scanning = False
        self.scan_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        for button in (self.export_button, self.autosync_button, self.classify_button, self.report_now_button):
            button.configure(state="normal")
        for switch in self.source_switches:
            switch.configure(state="normal")
        self.phase_label.configure(text="扫描失败")
        self.status_label.configure(text="扫描失败，详见弹窗。")
        logging.getLogger(__name__).error("扫描失败：\n%s", detail)
        messagebox.showerror("扫描失败", detail.strip().splitlines()[-1] if detail.strip() else "未知错误")

    # ================================================================ 表格
    def _visible_mods(self) -> List[ModResult]:
        if self.report is None:
            return []
        mods = list(self.report.mods)
        choice = self.filter_var.get()
        if choice == FILTER_CLIENT:
            mods = [mod for mod in mods if mod.side == SIDE_CLIENT]
        elif choice == FILTER_SERVER:
            mods = [mod for mod in mods if mod.side == SIDE_SERVER]
        elif choice == FILTER_BOTH:
            mods = [mod for mod in mods if mod.side == SIDE_BOTH]
        elif choice == FILTER_UNKNOWN:
            mods = [mod for mod in mods if mod.side == SIDE_UNKNOWN]
        elif choice == FILTER_CONFLICT:
            mods = [mod for mod in mods if mod.conflict]
        elif choice == FILTER_REVIEW:
            mods = [mod for mod in mods if mod.needs_review]

        keyword = self.search_var.get().strip().lower()
        if keyword:
            mods = [
                mod
                for mod in mods
                if keyword in (mod.name or "").lower()
                or keyword in (mod.mod_id or "").lower()
                or keyword in (mod.display_name or "").lower()
            ]

        if self._sort_col:
            mods.sort(key=_sort_key(self._sort_col), reverse=self._sort_desc)
        return mods

    def _tags_for(self, mod: ModResult, index: int) -> Tuple[str, ...]:
        """决定行的配色标签。

        Treeview 里**后列出的 tag 覆盖先列出的**，所以高优先级放最后：
        冲突 > 不确定 > 需人工确认 > 已人工确认 > 斑马纹。
        """
        tags: List[str] = ["odd" if index % 2 else "even"]
        if mod.manual:
            tags.append("manual")
        if mod.needs_review:
            tags.append("review")
        if mod.side == SIDE_UNKNOWN:
            tags.append("unknown")
        if mod.conflict:
            tags.append("conflict")
        return tuple(tags)

    def _refresh_table(self) -> None:
        tree = self.tree
        tree.delete(*tree.get_children())
        self._row_mod.clear()
        if self.report is None:
            self.count_label.configure(text="0 / 0")
            return

        mods = self._visible_mods()
        limit = max(0, int(self.config.ui_max_rows or 0))
        shown = mods if limit <= 0 else mods[:limit]
        for index, mod in enumerate(shown):
            iid = f"r{index}"
            self._row_mod[iid] = mod
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    mod.name,
                    mod.mod_id,
                    mod.display_name,
                    mod.side_label,
                    mod.confidence_label,
                    mod.source_summary,
                    mod.notes,
                ),
                tags=self._tags_for(mod, index),
            )
        total = len(self.report.mods)
        suffix = f"（表格仅显示前 {limit} 行）" if limit and len(mods) > limit else ""
        self.count_label.configure(text=f"{len(shown)} / {len(mods)}（共 {total}）{suffix}")

    def _sort_by(self, column: str) -> None:
        if self._sort_col == column:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = column
            self._sort_desc = False
        self._sync_sort_heading()
        self._refresh_table()

    def _sync_sort_heading(self) -> None:
        for col_id, title, _width, _anchor in COLUMNS:
            if col_id == self._sort_col:
                arrow = " ▼" if self._sort_desc else " ▲"
            else:
                arrow = ""
            self.tree.heading(col_id, text=f"{title}{arrow}")

    def _update_status(self) -> None:
        if self.report is None:
            return
        counts = self.report.counts
        self.status_label.configure(
            text=(
                f"目录：{self.report.mods_dir} ｜ 共 {len(self.report.mods)} 个 ｜ "
                f"纯客户端 {counts.get(SIDE_CLIENT, 0)} / 双端 {counts.get(SIDE_BOTH, 0)} / "
                f"纯服务端 {counts.get(SIDE_SERVER, 0)} / 不确定 {counts.get(SIDE_UNKNOWN, 0)} ｜ "
                f"冲突 {self.report.conflict_count} ｜ 需人工确认 {self.report.review_count} ｜ "
                f"已人工确认 {self.report.manual_count} ｜ 耗时 {self.report.elapsed:.1f}s ｜ "
                f"错误 {len(self.report.errors)} 条"
            )
        )

    # ================================================================ 人工修正
    def _on_row_double_click(self, event: Any) -> None:
        row = self.tree.identify_row(event.y)
        mod = self._row_mod.get(row)
        if mod is not None:
            self._edit_mod(mod)

    def _edit_selected_row(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        mod = self._row_mod.get(selection[0])
        if mod is not None:
            self._edit_mod(mod)

    def _edit_mod(self, mod: ModResult) -> None:
        chosen = _ask_side_dialog(self, mod)
        if chosen is None:
            return
        side = normalize_side(chosen)

        # ---- 就地更新这一行 ----
        mod.side = side
        mod.confidence = "high"
        mod.manual = True
        mod.needs_review = False
        mod.conflict = False
        mod.notes = f"人工确认：{side_label(side)}（GUI 人工确认）"
        mod.sources["manual"] = {
            "side": side,
            "confidence": "high",
            "source": "manual",
            "detail": mod.notes,
            "raw": {},
        }

        # ---- 写进缓存：sha1 精确到文件，modId 跨版本兜底 ----
        warning = ""
        if self.cache is None:
            warning = "缓存尚未初始化（请先完成一次扫描）"
        elif not (mod.sha1 or mod.mod_id):
            warning = "该 jar 既没有 sha1 也没有 modId，无法写入缓存"
        else:
            try:
                self.cache.put_override(side, sha1=mod.sha1, mod_id=mod.mod_id, note="GUI 人工确认")
                self.cache.save()
            except Exception as exc:  # pragma: no cover
                warning = f"写入缓存失败：{exc!r}"

        self._refresh_table()
        self._update_status()
        self.file_label.configure(text=f"已人工确认：{mod.name} -> {side_label(side)}")
        if warning:
            messagebox.showwarning("缓存未写入", f"{warning}\n\n本次修改只在当前会话内生效。")

    # ================================================================ 导出
    def _require_report(self) -> bool:
        if self.report is None or not self.report.mods:
            messagebox.showinfo("暂无结果", "请先完成一次扫描。")
            return False
        return True

    def _default_out_dir(self) -> str:
        """当前生效的导出目录：界面上填了就用它，否则回落到配置值 / 扫描目录。"""
        mods_dir = Path(self.report.mods_dir) if self.report and self.report.mods_dir else Path.cwd()
        try:
            typed = self.out_dir_var.get().strip()
        except (AttributeError, tk.TclError):
            typed = ""
        configured = typed or str(self.config.output_dir or "")
        return str(resolve_output_dir(configured, mods_dir))

    def _pick_output_dir(self) -> None:
        """选择常驻的导出目录（写回 config.json）。"""
        current = self.out_dir_var.get().strip() or self._default_out_dir()
        chosen = filedialog.askdirectory(title="选择报告导出目录", initialdir=current, mustexist=False)
        if not chosen:
            return
        self.out_dir_var.set(chosen)
        self.config.output_dir = chosen
        self._save_config()
        self.file_label.configure(text=f"导出目录：{chosen}")

    def _reset_output_dir(self) -> None:
        """清空导出目录设置，回到「导出到扫描目录」的默认行为。"""
        self.out_dir_var.set("")
        self.config.output_dir = ""
        self._save_config()
        self.file_label.configure(text="导出目录已恢复默认：写到扫描目录（mods/）")

    def export_reports(self) -> None:
        """导出 side-report.json / .csv / .txt 三件套。

        界面上**已设定**导出目录时直接用它（一键导出，不再弹框）；
        留空才弹目录选择框 —— 这样既保住「零配置」的默认路径，又允许固定输出位置。
        """
        if not self._require_report():
            return
        assert self.report is not None
        configured = self.out_dir_var.get().strip()
        if configured:
            out_dir = configured
        else:
            out_dir = filedialog.askdirectory(
                title="选择报告输出目录", initialdir=self._default_out_dir(), mustexist=False
            )
            if not out_dir:
                return
        try:
            paths = write_all(self.report, Path(out_dir))
        except OSError as exc:
            messagebox.showerror("导出失败", f"写入报告时出错：\n{exc!r}")
            return
        self.config.output_dir = out_dir
        self.out_dir_var.set(out_dir)
        self._save_config()
        # 记住这份报告，之后「立即上报」可以直接发它
        if paths:
            self._autosync_report_path = Path(paths[0])
        messagebox.showinfo("导出完成", "已写出：\n\n" + "\n".join(str(path) for path in paths))

    def export_to_autosync(self) -> None:
        """把 ``side-report.json`` 写进 AutoSync 的数据目录（方式 A 联动）。"""
        if not self._require_report():
            return
        assert self.report is not None
        out_dir = filedialog.askdirectory(
            title="选择 AutoSync 数据目录（会写入 side-report.json）",
            initialdir=self._default_out_dir(),
            mustexist=False,
        )
        if not out_dir:
            return
        try:
            path = write_json(self.report, Path(out_dir) / JSON_NAME, payload=autosync_payload(self.report))
        except OSError as exc:
            messagebox.showerror("导出失败", f"写入 {JSON_NAME} 时出错：\n{exc!r}")
            return
        self._autosync_report_path = Path(path)
        messagebox.showinfo(
            "导出完成",
            f"已写出 AutoSync 联动文件：\n{path}\n\n"
            "AutoSync 的 classify 优先读取该文件，读不到再退回查询 Modrinth。",
        )

    # ================================================================ 一键分类
    def classify_copy_action(self) -> None:
        """一键分类：**默认复制，绝不移动**，并且要二次确认。"""
        if not self._require_report():
            return
        assert self.report is not None
        out_dir = filedialog.askdirectory(
            title="选择分类输出目录（只复制，不移动原文件）",
            initialdir=self._default_out_dir(),
            mustexist=False,
        )
        if not out_dir:
            return

        confirmed = messagebox.askyesno(
            "确认一键分类（只复制，不移动）",
            f"将把 {len(self.report.mods)} 个 jar 「复制」到：\n{out_dir}\n\n"
            "会创建三个子目录：\n"
            "  client-mods/  纯客户端\n"
            "  server-mods/  纯服务端\n"
            "  both-mods/    双端 + 不确定（保守）\n\n"
            "注意：只复制，不移动 —— 原 mods 目录里的文件一律不动。\n"
            "目标目录已有同名但内容不同的文件时，默认不覆盖。\n\n确定继续吗？",
        )
        if not confirmed:
            return

        self._classifying = True
        self.classify_button.configure(state="disabled")
        self.phase_label.configure(text="正在复制…")
        self.file_label.configure(text=f"目标目录：{out_dir}")
        self.progress.set(0)
        self.after(POLL_MS, self._pump)

        report = self.report
        mods_dir = Path(report.mods_dir)
        target = Path(out_dir)

        def worker() -> None:
            try:
                result = classify_copy(report, mods_dir, target, overwrite=False)
                self._events.put(("classify_done", (result, "")))
            except Exception:  # pragma: no cover
                self._events.put(("classify_done", (None, traceback.format_exc())))

        threading.Thread(target=worker, name="ModSideDetectorClassify", daemon=True).start()

    def _on_classify_done(self, result: Any, error: str) -> None:
        self._classifying = False
        self.classify_button.configure(state="normal")
        self.progress.set(1.0)
        self.phase_label.configure(text="分类完成")
        if error or result is None:
            self.file_label.configure(text="")
            messagebox.showerror("分类失败", (error or "未知错误").strip().splitlines()[-1])
            return
        self.file_label.configure(text=result.summary_text())
        detail = result.summary_text()
        if result.conflicts:
            detail += "\n\n【同名但内容不同，未覆盖】\n" + "\n".join(result.conflicts[:10])
        if result.errors:
            detail += "\n\n【错误】\n" + "\n".join(result.errors[:10])
        messagebox.showinfo("一键分类完成", detail + f"\n\n输出目录：{result.out_dir}")

    # ================================================================ 关闭
    def _on_window_close(self) -> None:
        if self._scanning and not messagebox.askyesno("正在扫描", "扫描尚未完成，确定要退出吗？"):
            return
        self._cancel_event.set()
        self.config.mods_dir = self.path_var.get().strip() or self.config.mods_dir
        self._save_config()
        if self._on_close_hook is not None:
            try:
                self._on_close_hook()
            except Exception:  # pragma: no cover
                pass
        self.destroy()


# ---------------------------------------------------------------- 人工修正弹窗
def _ask_side_dialog(parent: "MainWindow", mod: ModResult) -> Optional[str]:
    """弹出侧别选择框；返回 ``client`` / ``server`` / ``both`` / ``unknown``，取消返回 None。"""
    from ..verdict import SIDE_LABELS as _LABELS

    label_to_side = {_LABELS[key]: key for key in (SIDE_CLIENT, SIDE_SERVER, SIDE_BOTH, SIDE_UNKNOWN)}
    dialog = ctk.CTkToplevel(parent)
    dialog.title("人工修正判定")
    dialog.geometry("560x330")
    dialog.resizable(False, False)
    dialog.transient(parent)

    result: Dict[str, Optional[str]] = {"side": None}

    ctk.CTkLabel(
        dialog,
        text=f"{mod.name}\nmodId: {mod.mod_id or '?'} ｜ 当前判定：{mod.side_label}"
        f" ｜ 置信度：{mod.confidence_label}",
        justify="left",
        anchor="w",
        wraplength=520,
        font=ctk.CTkFont(size=12),
    ).pack(fill="x", padx=16, pady=(16, 6))

    if mod.notes:
        ctk.CTkLabel(
            dialog,
            text=f"依据：{mod.notes}",
            justify="left",
            anchor="w",
            wraplength=520,
            text_color=("gray35", "gray65"),
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=16, pady=(0, 10))

    ctk.CTkLabel(
        dialog,
        text="请选择人工判定结果：",
        anchor="w",
        font=ctk.CTkFont(size=12, weight="bold"),
    ).pack(fill="x", padx=16, pady=(6, 4))

    chosen = tk.StringVar(value=_LABELS.get(mod.side, SIDE_LABELS[SIDE_UNKNOWN]))
    segmented = ctk.CTkSegmentedButton(
        dialog, values=list(label_to_side.keys()), variable=chosen, width=500
    )
    segmented.pack(padx=16, pady=6)

    ctk.CTkLabel(
        dialog,
        text=(
            "说明：人工确认会写入本地缓存（按 sha1 + modId 记录），\n"
            "之后扫描同一文件一律沿用该结论，不再受数据源影响。\n"
            "铁律提醒：拿不准时请选「双端」，选「纯服务端」会让客户端缺 mod 崩游戏。"
        ),
        justify="left",
        anchor="w",
        wraplength=520,
        text_color=("gray35", "gray65"),
        font=ctk.CTkFont(size=11),
    ).pack(fill="x", padx=16, pady=(10, 6))

    buttons = ctk.CTkFrame(dialog, fg_color="transparent")
    buttons.pack(fill="x", padx=16, pady=(4, 14))

    def confirm() -> None:
        result["side"] = label_to_side.get(chosen.get(), SIDE_UNKNOWN)
        dialog.destroy()

    def cancel() -> None:
        result["side"] = None
        dialog.destroy()

    ctk.CTkButton(buttons, text="取消", width=100, fg_color="transparent", border_width=1, command=cancel).pack(
        side="right", padx=(8, 0)
    )
    ctk.CTkButton(buttons, text="确认修改", width=100, command=confirm).pack(side="right")

    # 居中到父窗口，并抢占焦点（对话框模态）
    dialog.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:  # pragma: no cover
        pass
    dialog.grab_set()
    dialog.focus_force()
    parent.wait_window(dialog)
    return result["side"]


def run(
    config_path: Optional[Path] = None,
    mods_dir: Optional[str] = None,
    auto_scan: bool = False,
) -> int:
    """创建窗口并进入主循环；返回进程退出码。"""
    app = MainWindow(config_path=config_path, mods_dir=mods_dir, auto_scan=auto_scan)
    app.mainloop()
    return 0
