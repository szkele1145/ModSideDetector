#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ModSideDetector 文档站生成器。

把项目根目录/`docs` 下的 Markdown 源文件转成静态 HTML 站点，输出到 `docs/`
（GitHub Pages 从 main 分支的 /docs 目录发布）。

设计要点：
- 只用标准库 + 已安装的 `Markdown`（3.11），**不依赖 Pygments**；
  代码高亮由本文件内的轻量扫描器完成，配色在 `docs/assets/site.css` 里；
- **不使用任何外部 CDN 资源**（字体只用系统字体栈），离线也能正常渲染；
- 幂等：重复运行产出完全一致；只写 HTML，不碰 `docs/*.md` 源文件。

用法：
    python tools/build_site.py            # 生成站点到 docs/
    python tools/build_site.py --check    # 只校验，不写文件
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

import markdown

# --------------------------------------------------------------------------
# 基本路径与常量
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
ASSETS_DIR = DOCS_DIR / "assets"
REPO_URL = "https://github.com/szkele1145/ModSideDetector"
AUTOSYNC_URL = "https://github.com/szkele1145/AutoSync"
PAGES_URL = "https://szkele1145.github.io/ModSideDetector/"
MCMOD_URL = "https://www.mcmod.cn/"
MODRINTH_API_URL = "https://docs.modrinth.com/api/"

SITE_NAME = "ModSideDetector"
SITE_TAGLINE = "检测 Minecraft 模组「运行侧别」的 Windows 桌面工具"

# 顶部导航（顺序即显示顺序）
NAV = [
    ("index.html", "首页"),
    ("guide.html", "使用指南"),
    ("strategy.html", "判定策略"),
    ("handoff.html", "技术交接"),
    ("prompt.html", "原始需求书"),
]

# 需要从 Markdown 生成的页面
SOURCE_PAGES = [
    {
        "out": "guide.html",
        "nav": "使用指南",
        "src": "README.md",
        "title": "使用指南",
        "desc": "ModSideDetector 使用指南：快速开始、CLI 开关、判定结论读法、"
                "数据源覆盖率、打包与配置、已知限制。",
        "lead": "本页是 README.md 的网页版：从 GUI / CLI 快速开始，"
                "含全部命令行开关、配置项、打包说明与已知限制。",
    },
    {
        "out": "strategy.html",
        "nav": "判定策略",
        "src": "docs/判定策略.md",
        "title": "判定策略",
        "desc": "ModSideDetector 判定规则：枚举与置信度、数据源权重、"
                "融合规则、写进代码的铁律，以及实现阶段新踩到的坑。",
        "lead": "本页是 docs/判定策略.md 的网页版：判定枚举、数据源权重、"
                "多源融合规则与实现阶段新发现的坑。",
    },
    {
        "out": "handoff.html",
        "nav": "技术交接",
        "src": "HANDOFF.md",
        "title": "技术交接文档",
        "desc": "ModSideDetector 技术交接：数据源实测覆盖率与准确率结论、"
                "多源融合策略、AutoSync 联动方案与红线。",
        "lead": "本页是 HANDOFF.md 的网页版：全部实测结论（覆盖率、准确率）、"
                "可复用实现片段与不可触碰的红线。",
    },
    {
        "out": "prompt.html",
        "nav": "原始需求书",
        "src": "PROMPT.md",
        "title": "原始需求书",
        "desc": "ModSideDetector 的原始开发提示词与验收标准。",
        "lead": "本页是 PROMPT.md 的网页版：最初的开发提示词、功能清单、"
                "技术约束与验收标准。",
    },
]

# Markdown 里的链接 → 站点内页面（避免跳到 GitHub 看文档）
LINK_MAP = {
    "README.md": "guide.html",
    "./README.md": "guide.html",
    "docs/判定策略.md": "strategy.html",
    "./docs/判定策略.md": "strategy.html",
    "HANDOFF.md": "handoff.html",
    "PROMPT.md": "prompt.html",
    "LICENSE": f"{REPO_URL}/blob/main/LICENSE",
    "docs/README-PAGES.md": f"{REPO_URL}/blob/main/docs/README-PAGES.md",
}

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%230b0f12'/%3E"
    "%3Cpath d='M7 22l9-13 9 13' fill='none' stroke='%233ddc97' stroke-width='3'"
    " stroke-linecap='round' stroke-linejoin='round'/%3E"
    "%3Ccircle cx='16' cy='24' r='2' fill='%2338bdf8'/%3E%3C/svg%3E"
)


# --------------------------------------------------------------------------
# 轻量语法高亮（自写扫描器，不依赖 Pygments）
# --------------------------------------------------------------------------

_LANG_CONFIG = {
    "python": {
        "label": "Python",
        "line_comment": ("#",),
        "block_comment": (),
        "triple": ('"""', "'''"),
        "quotes": ('"', "'"),
        "prefixes": "rbfuRBFU",
        "variable_prefix": False,
        "param_prefix": False,
        "key_strings": False,
        "table_header": False,
        "keywords": {
            "False", "None", "True", "and", "as", "assert", "async", "await",
            "break", "class", "continue", "def", "del", "elif", "else", "except",
            "finally", "for", "from", "global", "if", "import", "in", "is",
            "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "self",
            "try", "while", "with", "yield",
        },
    },
    "powershell": {
        "label": "PowerShell",
        "line_comment": ("#",),
        "block_comment": (("<#", "#>"),),
        "triple": (),
        "quotes": ('"', "'"),
        "prefixes": "",
        "variable_prefix": True,
        "param_prefix": True,
        "key_strings": False,
        "table_header": False,
        "keywords": {
            "begin", "break", "catch", "class", "continue", "do", "else",
            "elseif", "end", "enum", "exit", "filter", "finally", "for",
            "foreach", "function", "if", "in", "param", "process", "return",
            "switch", "throw", "trap", "try", "until", "using", "while",
            "false", "null", "true",
        },
    },
    "json": {
        "label": "JSON",
        "line_comment": (),
        "block_comment": (),
        "triple": (),
        "quotes": ('"',),
        "prefixes": "",
        "variable_prefix": False,
        "param_prefix": False,
        "key_strings": True,
        "table_header": False,
        "keywords": {"true", "false", "null"},
    },
    "toml": {
        "label": "TOML",
        "line_comment": ("#",),
        "block_comment": (),
        "triple": ('"""', "'''"),
        "quotes": ('"', "'"),
        "prefixes": "",
        "variable_prefix": False,
        "param_prefix": False,
        "key_strings": True,
        "table_header": True,
        "keywords": {"true", "false"},
    },
    "ini": {
        "label": "INI",
        "line_comment": ("#", ";"),
        "block_comment": (),
        "triple": (),
        "quotes": ('"', "'"),
        "prefixes": "",
        "variable_prefix": False,
        "param_prefix": False,
        "key_strings": True,
        "table_header": True,
        "keywords": set(),
    },
    "text": {
        "label": "文本",
        "line_comment": (),
        "block_comment": (),
        "triple": (),
        "quotes": (),
        "prefixes": "",
        "variable_prefix": False,
        "param_prefix": False,
        "key_strings": False,
        "table_header": False,
        "keywords": set(),
    },
}

# 常见语言标记的别名
_LANG_ALIAS = {
    "py": "python",
    "python3": "python",
    "ps1": "powershell",
    "ps": "powershell",
    "pwsh": "powershell",
    "console": "powershell",
    "shell": "powershell",
    "bat": "powershell",
    "md": "text",
    "markdown": "text",
    "": "text",
}


def _span(cls: str, text: str) -> str:
    """生成高亮片段；cls 为空则只做 HTML 转义。"""
    escaped = html.escape(text, quote=False)
    if not cls:
        return escaped
    return f'<span class="hl-{cls}">{escaped}</span>'


def _scan_quoted(code: str, i: int, quote: str, cfg: dict) -> int:
    """从位置 i 的引号开始扫描字符串，返回结束位置（不含）。"""
    n = len(code)
    if quote in cfg["triple"] and code.startswith(quote * 3, i):
        end = code.find(quote * 3, i + 3)
        return n if end < 0 else end + 3
    j = i + 1
    while j < n:
        ch = code[j]
        if ch in ("\\", "`"):  # Python 用反斜杠，PowerShell 用反引号
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n":  # 单行字符串不跨行
            return j
        j += 1
    return n


def highlight(code: str, lang: str) -> str:
    """把源码转成带 <span class="hl-*"> 的 HTML（已转义，可安全内嵌）。"""
    key = (lang or "").strip().lower()
    key = _LANG_ALIAS.get(key, key)
    cfg = _LANG_CONFIG.get(key, _LANG_CONFIG["text"])

    out: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        line_start = i == 0 or code[i - 1] == "\n"
        consumed = False

        # 1) 行注释
        for tok in cfg["line_comment"]:
            if code.startswith(tok, i):
                j = code.find("\n", i)
                j = n if j < 0 else j
                out.append(_span("c", code[i:j]))
                i, consumed = j, True
                break
        if consumed:
            continue

        # 2) 块注释
        for start, end in cfg["block_comment"]:
            if code.startswith(start, i):
                j = code.find(end, i + len(start))
                j = n if j < 0 else j + len(end)
                out.append(_span("c", code[i:j]))
                i, consumed = j, True
                break
        if consumed:
            continue

        # 3) TOML 表头 [section]
        if cfg["table_header"] and ch == "[" and code[:i].split("\n")[-1].strip() == "":
            j = code.find("]", i)
            if j > 0:
                out.append(_span("t", code[i:j + 1]))
                i, consumed = j + 1, True
        if consumed:
            continue

        # 4) 变量（PowerShell $var / $env:NAME）
        if cfg["variable_prefix"] and ch == "$":
            m = re.match(r"\$[\w:]+", code[i:])
            if m:
                out.append(_span("v", m.group(0)))
                i += len(m.group(0))
                continue

        # 5) 参数（PowerShell -Flag）
        if cfg["param_prefix"] and ch == "-":
            m = re.match(r"-[A-Za-z][\w-]*", code[i:])
            if m:
                out.append(_span("k", m.group(0)))
                i += len(m.group(0))
                continue

        # 6) 字符串（含 Python 的 r"..." 前缀）
        if ch in cfg["quotes"]:
            j = _scan_quoted(code, i, ch, cfg)
            cls = "s"
            if cfg["key_strings"]:
                k = j
                while k < n and code[k] in " \t":
                    k += 1
                if k < n and code[k] == ":":
                    cls = "a"  # JSON / TOML 的键名
            out.append(_span(cls, code[i:j]))
            i = j
            continue
        if cfg["prefixes"] and ch in cfg["prefixes"]:
            m = re.match(r"[rbfuRBFU]{1,2}(?=['\"])", code[i:])
            if m:
                q = code[i + len(m.group(0))]
                j = _scan_quoted(code, i + len(m.group(0)), q, cfg)
                out.append(_span("s", code[i:j]))
                i = j
                continue

        # 7) 数字
        if ch in "0123456789":
            m = re.match(r"0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?", code[i:])
            if m:
                out.append(_span("n", m.group(0)))
                i += len(m.group(0))
                continue

        # 8) 标识符 / 关键字 / 键名（只处理 ASCII 标识符，中文原样输出）
        if ch.isascii() and (ch.isalpha() or ch == "_"):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", code[i:])
            word = m.group(0)
            cls = ""
            if word in cfg["keywords"]:
                cls = "k"
            else:
                k = i + len(word)
                while k < n and code[k] in " \t":
                    k += 1
                if k < n and code[k] in "=:":
                    cls = "a"  # 键名 / 赋值目标
            out.append(_span(cls, word))
            i += len(word)
            continue

        # 9) 其它字符
        out.append(html.escape(ch, quote=False))
        i += 1

        # 行首标记只与上一行有关，上面每次循环开头已经重算

    return "".join(out)


def code_block(lang: str, code: str, label: str | None = None) -> str:
    """生成带语言标签 + 复制按钮的代码块。"""
    key = (lang or "").strip().lower()
    key = _LANG_ALIAS.get(key, key)
    cfg = _LANG_CONFIG.get(key, _LANG_CONFIG["text"])
    shown = label or cfg["label"]
    inner = highlight(code.rstrip("\n"), key)
    lang_class = f' class="language-{html.escape(key)}"' if key and key != "text" else ""
    return (
        '<div class="codeblock">'
        '<div class="codeblock-head">'
        f'<span class="code-lang">{html.escape(shown)}</span>'
        '<button class="copy-btn" type="button" data-copy-btn>复制</button>'
        "</div>"
        f"<pre><code{lang_class}>{inner}</code></pre>"
        "</div>"
    )


# --------------------------------------------------------------------------
# Markdown 渲染
# --------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def slugify_cn(value, separator, unicode: bool = False) -> str:
    """保留中文的锚点 slug（默认 slugify 会把中文全部丢掉）。"""
    text = html.unescape(str(value)).strip().lower()
    text = _SLUG_STRIP.sub(separator, text)
    return text.strip(separator) or "section"


_FENCE_RE = re.compile(
    r'<pre><code(?: class="language-([\w+#.-]+)")?>(.*?)</code></pre>',
    re.S,
)
_HEADING_RE = re.compile(r"<(h[1-6])>(.*?)</\1>", re.S)


def _highlight_fenced_blocks(body: str) -> str:
    """把 fenced_code 输出的 <pre><code> 升级成带头部与高亮的代码块。"""

    def repl(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        raw = html.unescape(m.group(2))
        return code_block(lang, raw)

    return _FENCE_RE.sub(repl, body)


def _wrap_tables(body: str) -> str:
    """给表格套一层可横向滚动的容器，避免窄屏撑破布局。"""
    return body.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )


def _rewrite_links(body: str) -> str:
    """重写链接：项目内 md → 站点页面；外链新窗口打开。"""

    def repl(m: re.Match[str]) -> str:
        href = html.unescape(m.group(1))
        target = LINK_MAP.get(href)
        if target is not None:
            return f'<a href="{html.escape(target, quote=True)}"'
        if href.startswith(("http://", "https://")):
            return (
                f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer"'
            )
        return m.group(0)

    body = re.sub(r'<a href="([^"]*)"', repl, body)
    # 站点产物都落在 docs/ 下，而 Markdown 里的相对路径是**相对仓库根**写的
    # （例如 README 里 `docs/assets/gui-preview.png`）。在 docs/guide.html 里
    # 必须剥掉 `docs/` 前缀，否则图片 404。
    body = re.sub(r'(src=")docs/', r"\1", body)
    return body


def render_markdown(raw: str) -> tuple[str, str]:
    """返回 (正文 HTML, 目录 HTML)。"""
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists"],
        extension_configs={
            "toc": {
                "slugify": slugify_cn,
                "separator": "-",
                "toc_depth": "1-4",
                "anchorlink": False,
                "permalink": False,
            },
            "fenced_code": {},
        },
        output_format="html5",
    )
    body = md.convert(raw)
    toc = getattr(md, "toc", "") or ""
    body = _highlight_fenced_blocks(body)
    body = _wrap_tables(body)
    body = _rewrite_links(body)
    return body, toc


# --------------------------------------------------------------------------
# 页面模板
# --------------------------------------------------------------------------

HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta name="color-scheme" content="dark">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="website">
<link rel="icon" href="{favicon}">
<link rel="stylesheet" href="assets/site.css">
</head>
<body>
<a class="skip-link" href="#main">跳到正文</a>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="index.html">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="26" height="26"><path d="M6 23l10-14 10 14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="16" cy="25" r="2.2" fill="#38bdf8"/></svg>
      </span>
      <span class="brand-text">
        <span class="brand-name">ModSideDetector</span>
        <span class="brand-sub">模组运行侧别检测</span>
      </span>
    </a>
    <button class="nav-toggle" type="button" aria-expanded="false" aria-controls="site-nav" data-nav-toggle>
      <span class="nav-toggle-bar" aria-hidden="true"></span>
      <span class="sr-only">切换导航</span>
    </button>
    <nav class="site-nav" id="site-nav" data-nav>
{nav}
      <a class="nav-gh" href="{repo}" target="_blank" rel="noopener noreferrer">GitHub</a>
    </nav>
  </div>
</header>
<main id="main">
"""

FOOT = """</main>
<footer class="site-footer">
  <div class="footer-inner">
    <div class="footer-col">
      <div class="footer-brand">ModSideDetector</div>
      <p class="footer-note">检测 Minecraft 模组运行侧别的桌面工具：判断纯客户端 / 纯服务端 / 双端，并导出 <code>side-report.json</code> 供 AutoSync 消费。</p>
    </div>
    <div class="footer-col">
      <div class="footer-title">站点</div>
      <ul class="footer-list">
{footer_links}
      </ul>
    </div>
    <div class="footer-col">
      <div class="footer-title">相关</div>
      <ul class="footer-list">
        <li><a href="{repo}" target="_blank" rel="noopener noreferrer">GitHub 仓库</a></li>
        <li><a href="{autosync}" target="_blank" rel="noopener noreferrer">AutoSync 项目</a></li>
        <li><a href="{mcmod}" target="_blank" rel="noopener noreferrer">MC 百科（mcmod.cn）</a></li>
        <li><a href="{modrinth}" target="_blank" rel="noopener noreferrer">Modrinth API 文档</a></li>
      </ul>
    </div>
  </div>
  <div class="footer-legal">
    <p><strong>许可说明</strong>：本项目管理器代码采用 <a href="{license}" target="_blank" rel="noopener noreferrer">MIT 许可</a>。mcmod.cn 的内容为 <strong>BY-NC-SA 3.0</strong>（禁止商业使用、转载需署名），与本项目的 MIT 许可无关，不要混淆；缓存数据不得随分发产物一起分发。</p>
    <p class="footer-meta">文档由 <code>tools/build_site.py</code> 从项目内 Markdown 生成 · 无任何外部 CDN 依赖 · 离线可读</p>
  </div>
</footer>
<button class="to-top" type="button" data-to-top aria-label="回到顶部">↑</button>
<script src="assets/site.js" defer></script>
</body>
</html>
"""


def _nav_html(active: str) -> str:
    lines = []
    for href, label in NAV:
        if href == active:
            lines.append(
                f'      <a class="nav-link is-active" href="{href}" '
                f'aria-current="page">{label}</a>'
            )
        else:
            lines.append(f'      <a class="nav-link" href="{href}">{label}</a>')
    return "\n".join(lines)


def _footer_links() -> str:
    return "\n".join(
        f'        <li><a href="{href}">{label}</a></li>' for href, label in NAV
    )


def render_page(*, title: str, desc: str, main_html: str, active: str) -> str:
    head = HEAD.format(
        title=html.escape(title),
        desc=html.escape(desc, quote=True),
        favicon=FAVICON,
        nav=_nav_html(active),
        repo=REPO_URL,
    )
    foot = FOOT.format(
        footer_links=_footer_links(),
        repo=REPO_URL,
        autosync=AUTOSYNC_URL,
        mcmod=MCMOD_URL,
        modrinth=MODRINTH_API_URL,
        license=f"{REPO_URL}/blob/main/LICENSE",
    )
    return head + main_html + "\n" + foot


def render_doc_page(page: dict) -> str:
    src = ROOT / page["src"]
    if not src.is_file():
        raise FileNotFoundError(f"缺少 Markdown 源文件：{src}")
    raw = src.read_text(encoding="utf-8")
    body, toc = render_markdown(raw)
    toc_html = ""
    if "<ul>" in toc:
        toc_html = (
            '<aside class="doc-aside">\n'
            '<div class="aside-title">本页目录</div>\n'
            f"{toc}\n"
            "</aside>\n"
        )
    main = (
        '<section class="page-hero">\n'
        '  <div class="wrap">\n'
        f'    <p class="crumb"><a href="index.html">首页</a> / {html.escape(page["nav"])}</p>\n'
        f'    <h1 class="page-title">{html.escape(page["title"])}</h1>\n'
        f'    <p class="page-lead">{html.escape(page["lead"])}</p>\n'
        '    <p class="src-note">内容源：'
        f'<code>{html.escape(page["src"])}</code></p>\n'
        "  </div>\n"
        "</section>\n"
        '<section class="doc-section">\n'
        '  <div class="wrap doc-layout">\n'
        f"{toc_html}"
        f'    <article class="doc-content">\n{body}\n    </article>\n'
        "  </div>\n"
        "</section>\n"
    )
    return render_page(
        title=f'{page["title"]} · {SITE_NAME}',
        desc=page["desc"],
        main_html=main,
        active=page["out"],
    )


# --------------------------------------------------------------------------
# 首页（手写内容，素材全部来自项目内文档）
# --------------------------------------------------------------------------

def build_index_main() -> str:
    features = [
        ("多源交叉判定",
         "MC 百科（mcmod.cn）的「运行环境」字段 + Modrinth 的 <code>client_side</code> / "
         "<code>server_side</code> + jar 内静态特征，三个来源互相印证，而不是只听一家之言。"),
        ("保守优先，绝不冒进",
         "判不了就放双端。冲突时工具<strong>不做「谁更权威」的猜测</strong>，"
         "直接判双端并请人工确认 —— 猜错的代价是崩游戏。"),
        ("人工复核永久生效",
         "双击某行即可改判，结论写进缓存下次扫描直接沿用；人工结论<strong>永远最高优先级</strong>，"
         "不会被自动判定覆盖。"),
        ("CLI + GUI 双入口",
         "<code>python -m src</code> 一条命令跑批量扫描；"
         "CustomTkinter 界面提供进度、排序、筛选、冲突高亮与一键导出。"),
        ("一键分类，复制不移动",
         "按判定结果分出 <code>client-mods/</code> · <code>server-mods/</code> · "
         "<code>both-mods/</code>；默认<strong>只复制不移动</strong>，目标同名不同内容绝不覆盖。"),
        ("与 AutoSync 联动",
         "导出严格按约定格式的 <code>side-report.json</code>，AutoSync 的 classify 优先读它，"
         "读不到才回退查 Modrinth。"),
    ]
    feature_cards = "\n".join(
        '      <article class="card feature">'
        f'<h3 class="card-title">{title}</h3><p class="card-text">{text}</p></article>'
        for title, text in features
    )

    verdict_rows = [
        ("纯客户端", "<code>client</code>", "只客户端装", "只发给玩家"),
        ("纯服务端", "<code>server</code>", "只服务端装", "只放服务器"),
        ("双端", "<code>both</code>", "两边都要", "两边都装"),
        ("不确定", "<code>unknown</code>", "判不了", "按配置处理（默认当双端）"),
    ]
    verdict_html = "\n".join(
        f"          <tr><td><strong>{name}</strong></td><td>{code}</td>"
        f"<td>{mean}</td><td>{dist}</td></tr>"
        for name, code, mean, dist in verdict_rows
    )

    conf_rows = [
        ("高&nbsp;<code>high</code>",
         "两源一致，或某源给出<strong>明确依据</strong>（如 <code>client_side=unsupported</code>、"
         "mcmod 明写「某端无效」、<code>clientSideOnly=true</code>）"),
        ("中&nbsp;<code>medium</code>", "只有一个数据源有数据"),
        ("低&nbsp;<code>low</code>",
         "两源冲突 / 无数据源 / 弱信号兜底 / <code>server</code> 判定被铁律降级"),
    ]
    conf_html = "\n".join(
        f"          <tr><td>{level}</td><td>{desc}</td></tr>" for level, desc in conf_rows
    )

    source_rows = [
        ("mcmod「运行环境」", "92%",
         "HANDOFF.md 实测（12 / 13 抽样）。语义最清晰、<strong>质量最高</strong>，需限流抓取"
         "（0.6 秒/请求、串行）"),
        ("Modrinth", "79.5%",
         "本项目在 151 个模组上复测 <strong>120 / 151</strong>；HANDOFF.md 另一次实测为 "
         "121 / 150 = 80.7%。官方批量 API，快；但字段由作者自填，常不准"),
        ("jar 元数据", "约 6%",
         "<code>clientSideOnly</code> / Fabric <code>environment</code>：作者显式声明，可信但覆盖率低"),
        ("mixin / <code>data/</code> 启发式", "100% 覆盖",
         "<strong>只作提示，绝不单独定案</strong>（<code>displayTest</code> 实测 93% 未设置）"),
        ("人工复核", "100%",
         "一次性确认，写进缓存永久生效，优先级永远最高"),
    ]
    source_html = "\n".join(
        f"          <tr><td>{name}</td><td class=\"num\">{cov}</td><td>{desc}</td></tr>"
        for name, cov, desc in source_rows
    )

    redlines = [
        "不要把 mcmod 抓来的数据打包进分发的产物（BY-NC-SA 3.0）",
        "不要并发狂抓 mcmod（限流 0.6 秒/请求，串行）",
        "不要用字节码扫描做主判据（实测准确率仅约 55%）",
        "不要把「不确定」判成 <code>server</code>（代价是客户端缺 mod 崩游戏）",
        "不要重写 AutoSync 已有的 TOML / JiJ 解析",
    ]
    redline_html = "\n".join(f"        <li>{item}</li>" for item in redlines)

    quickstart = code_block(
        "powershell",
        'python -m src                           # 打开 GUI\n'
        'python -m src --mods-dir "D:\\mc\\mods"    # 打开并立即开始扫描\n'
        '\n'
        '# 命令行：扫描并导出 side-report.json / .csv / .txt\n'
        'python -m src scan "D:\\mc\\mods"\n'
        '\n'
        '# 额外写一份给 AutoSync 读的文件\n'
        'python -m src scan "D:\\mc\\mods" --for-autosync "D:\\AutoSync\\data"\n'
        '\n'
        '# 人工确认某个 mod（写进缓存，永久生效）\n'
        'python -m src review "D:\\mc\\mods" --name sodium.jar --side client --note "服主确认"',
    )

    autosync_json = code_block(
        "json",
        '{\n'
        '  "generated": "2026-10-06T17:30:00+08:00",\n'
        '  "tool": "ModSideDetector 1.0.0",\n'
        '  "mods": [\n'
        '    {\n'
        '      "file": "sodium-neoforge-0.8.13+mc1.21.1.jar",\n'
        '      "sha1": "…",\n'
        '      "sha256": "…",\n'
        '      "mod_id": "sodium",\n'
        '      "display_name": "Sodium",\n'
        '      "side": "client",\n'
        '      "confidence": "high",\n'
        '      "sources": {\n'
        '        "modrinth": { "client_side": "required", "server_side": "unsupported" },\n'
        '        "mcmod":    { "class_id": 332, "run_env": "客户端需装, 服务端需装" },\n'
        '        "heuristics": { "display_test": null, "has_client_mixin": true }\n'
        '      },\n'
        '      "notes": ""\n'
        '    }\n'
        '  ]\n'
        '}',
    )

    # 下面两个代码块在 f-string 之外先构造好：
    # 一是避免 f-string 内出现反斜杠（保持 Python 3.11 兼容），二是保证 \n 是真换行
    classify_dirs = code_block(
        "text",
        "<输出目录>/\n"
        "├── client-mods/     纯客户端（只发给玩家）\n"
        "├── server-mods/     纯服务端（只放服务器）\n"
        "└── both-mods/       双端 + 不确定（两边都要）",
    )
    build_hint = code_block(
        "powershell",
        "powershell -ExecutionPolicy Bypass -File build.ps1\n"
        "# 产物：dist\\ModSideDetector\\ModSideDetector.exe",
    )

    return f"""<section class="hero">
  <div class="wrap hero-inner">
    <span class="badge">Windows 桌面工具 · CLI + GUI · PyInstaller 打包</span>
    <h1 class="hero-title">ModSideDetector</h1>
    <p class="hero-lead">检测 Minecraft 模组<strong>运行侧别</strong>的桌面工具 —— 判断每个 mod 属于
      <strong>纯客户端</strong> / <strong>纯服务端</strong> / <strong>双端</strong>，
      并导出报告供 <a href="{AUTOSYNC_URL}" target="_blank" rel="noopener noreferrer">AutoSync</a> 直接消费。</p>
    <div class="pain-grid">
      <div class="pain pain-warn">
        <div class="pain-label">装多了</div>
        <div class="pain-text">玩家白下载几百 MB</div>
      </div>
      <div class="pain pain-danger">
        <div class="pain-label">装少了</div>
        <div class="pain-text">直接崩游戏（Missing or unsupported mandatory dependencies）</div>
      </div>
    </div>
    <div class="cta-row">
      <a class="btn btn-primary" href="guide.html">阅读使用指南</a>
      <a class="btn" href="strategy.html">看判定策略</a>
      <a class="btn" href="{REPO_URL}" target="_blank" rel="noopener noreferrer">GitHub 仓库</a>
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <h2 class="section-title">它解决什么问题</h2>
    <p class="section-lead">整合包作者手工分辨 mod 侧别极耗时且容易错。现有方案（Modrinth 的
      <code>client_side</code> / <code>server_side</code>）由 <strong>mod 作者自己填</strong>，经常不准 ——
      这正是本项目要改进的环节。</p>
    <p class="section-lead">ModSideDetector 的做法是<strong>多源交叉</strong>：MC 百科（mcmod.cn）的「运行环境」字段
      + Modrinth 的 side 字段 + jar 内静态特征，冲突时请人来定，判不了时<strong>保守放双端</strong>。</p>
    <div class="card-grid">
{feature_cards}
    </div>
  </div>
</section>

<section class="section section-alt">
  <div class="wrap">
    <h2 class="section-title">判定结论怎么读</h2>
    <p class="section-lead">工具输出的四种判定、对应的分发含义，以及三档置信度。</p>
    <div class="split">
      <div class="table-wrap">
        <table>
          <thead><tr><th>判定</th><th>枚举值</th><th>含义</th><th>分发含义</th></tr></thead>
          <tbody>
{verdict_html}
          </tbody>
        </table>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>置信度</th><th>含义</th></tr></thead>
          <tbody>
{conf_html}
          </tbody>
        </table>
      </div>
    </div>
    <p class="callout">界面上要重点看两类行：<code>冲突</code>（两源结论相反）与
      <code>需人工确认</code>。其余行扫一眼即可。冲突时工具不做「谁更权威」的猜测 —— 猜错的代价是崩游戏。</p>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <h2 class="section-title">数据源与实测覆盖率</h2>
    <p class="section-lead">在<strong>真实数据</strong>（151 个 NeoForge 1.21.1 模组，817 MB）上的实测结论。</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>数据源</th><th>覆盖率</th><th>说明</th></tr></thead>
        <tbody>
{source_html}
        </tbody>
      </table>
    </div>
    <p class="callout callout-warn"><strong>刻意不做的事</strong>：不扫字节码。实测字节码扫描准确率仅约 55%
      （<code>Axiom</code>、<code>Flashback</code>、<code>Xaero 小地图</code> 全被误判），只能当弱信号。</p>
  </div>
</section>

<section class="section section-alt">
  <div class="wrap">
    <h2 class="section-title">快速开始</h2>
    <p class="section-lead">要求 Python 3.11+；运行期只用标准库 + <code>customtkinter</code>，
      网络层是标准库 <code>urllib</code>（不依赖 <code>requests</code>）。</p>
    <div class="split split-code">
      <div>
{quickstart}
        <p class="callout">⚠️ 全局参数要放在子命令<strong>前面</strong>：<code>python -m src --verbose scan ...</code>
          是对的，<code>python -m src scan ... --verbose</code> 会报 <code>unrecognized arguments</code>（argparse 的行为）。</p>
      </div>
      <div class="side-note">
        <h3 class="mini-title">也支持双击运行</h3>
        <p>打包后直接双击 <code>dist\\ModSideDetector\\ModSideDetector.exe</code>，免装 Python：</p>
        {build_hint}
        <p class="mini-note">脚本采用 <code>--onedir</code>（不是 <code>--onefile</code>）+ <code>--windowed</code>，
          并排除 matplotlib / numpy / PyQt 等无关大件。</p>
        <p class="callout callout-warn">PyInstaller 打的 exe <strong>经常被杀毒软件误报</strong>，这是打包器的通用现象。
          缓解办法：用 <code>--onedir</code>、把 <code>dist\\ModSideDetector\\</code> <strong>整个目录</strong>加白名单
          （不要只加 exe）、或直接从源码跑。</p>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <h2 class="section-title">与 AutoSync 联动</h2>
    <p class="section-lead">AutoSync 的 <code>classify</code> 原先依据 Modrinth 的 side 字段分类 ——
      正是本项目要改进的那个环节。现在它<strong>优先读</strong> ModSideDetector 生成的报告。</p>
    <div class="flow">
      <div class="flow-node">
        <div class="flow-kicker">步骤 1</div>
        <div class="flow-title">扫描 mods 目录</div>
        <div class="flow-desc"><code>python -m src scan "D:\\mc\\mods" --for-autosync "D:\\AutoSync\\data"</code></div>
      </div>
      <div class="flow-arrow" aria-hidden="true">↓</div>
      <div class="flow-node flow-node-accent">
        <div class="flow-kicker">步骤 2</div>
        <div class="flow-title">生成 side-report.json</div>
        <div class="flow-desc">严格按 HANDOFF.md 4.2 节约定：<code>generated</code> / <code>tool</code> / <code>mods[]</code>，
          每条含 <code>file</code> / <code>sha1</code> / <code>sha256</code> / <code>mod_id</code> /
          <code>display_name</code> / <code>side</code> / <code>confidence</code> / <code>sources</code> / <code>notes</code></div>
      </div>
      <div class="flow-arrow" aria-hidden="true">↓</div>
      <div class="flow-node">
        <div class="flow-kicker">步骤 3</div>
        <div class="flow-title">AutoSync 的 classify 优先读它</div>
        <div class="flow-desc">读不到才回退去查 Modrinth。</div>
      </div>
    </div>
    <p class="section-lead">一键分类的输出目录名也与 AutoSync 的 <code>classify</code> 语义一致：</p>
    {classify_dirs}
    <div class="split split-code">
      <div>
        <h3 class="mini-title">side-report.json 结构（摘自 HANDOFF.md 4.2）</h3>
        {autosync_json}
      </div>
      <div class="side-note">
        <h3 class="mini-title">为什么值得联动</h3>
        <p>AutoSync 的 <code>classify.py</code> 把 <code>client-dist/mods/</code> 里的 mod 分成纯客户端 / 双端 / 纯服务端，
          再复制服务端需要的那些。换成读本工具的报告后，<strong>分类准确率大幅提升</strong>。</p>
        <h3 class="mini-title">红线</h3>
        <ul class="tick-list tick-list-danger">
{redline_html}
        </ul>
      </div>
    </div>
  </div>
</section>

<section class="section section-alt">
  <div class="wrap">
    <h2 class="section-title">已知限制</h2>
    <ul class="limit-list">
      <li><strong>Modrinth 字段由作者自填</strong>，可能整条都是错的；工具会在两源冲突时高亮，但最终仍需要人工过一遍冲突项与「需人工确认」项 —— 这是达到 100% 准确的唯一路径。</li>
      <li><strong>某些 mod 在任何数据源上都查不到</strong>（例如作者没发布到 Modrinth、mcmod 也没收录），此时只能保守判双端。</li>
      <li><strong>mcmod 新收录条目可能没有「运行环境」字段</strong>：这是「字段缺失」，不等于「无侧别信息」，工具会退回其它数据源。</li>
      <li><strong>首次全量扫描需要抓 mcmod</strong>，耗时与 mod 数量成正比（限流 0.6 秒/请求，151 个 mod 约 2–3 分钟）；第二次扫描走缓存，几秒完成。</li>
      <li><code>--windowed</code> 打包出的 exe 从命令行调用时，输出走的是父控制台而非管道，脚本里用管道捕获可能拿不到文字。</li>
    </ul>
    <p class="section-lead">更完整的细节见
      <a href="guide.html">使用指南</a>、<a href="strategy.html">判定策略</a> 与
      <a href="handoff.html">技术交接文档</a>。</p>
  </div>
</section>
"""


# --------------------------------------------------------------------------
# 校验与写入
# --------------------------------------------------------------------------

PLACEHOLDER_PATTERNS = [
    (re.compile(r"\{\{"), "模板占位符残留 {{"),
    (re.compile(r"\{%"), "模板占位符残留 {%"),
    (re.compile(r"\{#"), "模板占位符残留 {#"),
    (re.compile(r"@@[A-Z_]+@@"), "未替换的 @@占位符@@"),
    (re.compile(r"\bTODO\b"), "未处理的 TODO"),
    (re.compile(r"\bFIXME\b"), "未处理的 FIXME"),
    (re.compile(r"\bLorem ipsum\b", re.I), "占位文案 Lorem ipsum"),
]


def validate(name: str, text: str) -> list[str]:
    problems = []
    for pattern, desc in PLACEHOLDER_PATTERNS:
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            problems.append(f"{name}:{line} {desc} → {m.group(0)!r}")
    # 基础结构检查
    for tag in ("<!DOCTYPE html>", "</html>", 'lang="zh-CN"', 'assets/site.css'):
        if tag not in text:
            problems.append(f"{name}: 缺少必要片段 {tag!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 ModSideDetector 文档站（输出到 docs/）"
    )
    parser.add_argument("--check", action="store_true",
                        help="只校验生成结果，不写任何文件")
    parser.add_argument("--quiet", action="store_true", help="只在出错时输出")
    args = parser.parse_args(argv)

    if not ASSETS_DIR.is_dir():
        print(f"[错误] 缺少资源目录：{ASSETS_DIR}", file=sys.stderr)
        return 2
    for asset in ("site.css", "site.js"):
        if not (ASSETS_DIR / asset).is_file():
            print(f"[错误] 缺少资源文件：{ASSETS_DIR / asset}", file=sys.stderr)
            return 2

    outputs: dict[str, str] = {}

    index_main = build_index_main()
    outputs["index.html"] = render_page(
        title=f"{SITE_NAME} · {SITE_TAGLINE}",
        desc="检测 Minecraft 模组运行侧别的 Windows 桌面工具："
             "多源交叉判定纯客户端 / 纯服务端 / 双端，导出 side-report.json 供 AutoSync 消费。",
        main_html=index_main,
        active="index.html",
    )

    for page in SOURCE_PAGES:
        outputs[page["out"]] = render_doc_page(page)

    problems: list[str] = []
    for name, text in outputs.items():
        problems.extend(validate(name, text))

    if problems:
        print("[校验失败] 生成结果存在问题：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1

    if args.check:
        if not args.quiet:
            print("[--check] 校验通过，未写入任何文件。")
            for name in outputs:
                print(f"  ✓ {name}  ({len(outputs[name]):,} 字符)")
        return 0

    for name, text in outputs.items():
        target = DOCS_DIR / name
        # 幂等：内容一致时不重写文件（保持时间戳稳定）
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        target.write_text(text, encoding="utf-8", newline="\n")

    if not args.quiet:
        print(f"[完成] 站点已生成到 {DOCS_DIR}")
        for name in outputs:
            print(f"  · docs/{name}")
        print(f"  共 {len(outputs)} 个页面；源 Markdown 未被修改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
