"""MC 百科（mcmod.cn）数据源 —— **质量最高的侧别依据**。

模组详情页有 ``运行环境`` 字段，直接给出侧别：

===========================  ============
``客户端需装, 服务端无效``   纯客户端
``客户端可选, 服务端无效``   纯客户端
``服务端需装, 客户端无效``   纯服务端
``客户端需装, 服务端需装``   双端
``客户端需装, 服务端可选``   双端
``客户端可选, 服务端需装``   双端
``客户端可选, 服务端可选``   双端
``客户端需装``               只有半边信息 -> **保守判双端**
===========================  ============

**核心口径（项目所有者确认）：只有「无效」才算不需要；「需装」和「可选」一律
视为需要。** 理由与全局的保守原则一致 —— 代价不对等：多发一个 mod 只是浪费带宽，
漏发一个 mod 会让客户端/服务端直接崩游戏。因此「客户端需装 + 服务端可选」判**双端**，
而不是因为「服务端可选」就判纯客户端。

**硬约束（HANDOFF 实测）**

1. 限流 **0.6 秒/请求、串行**，不要并发 —— 尊重对方服务器；
2. 搜索匹配是最大难点（``架构师`` 在 mcmod 上叫 ``Architectury``），
   必须多策略重试：displayName 原文 -> 去括号 -> 括号内英文名 -> modId -> 文件名；
3. 新收录条目可能**没有** ``运行环境`` 字段 —— 这是「字段缺失」，不是「无侧别信息」；
4. 内容为 **BY-NC-SA 3.0**：本地缓存可以，**不要打包进分发的产物**；
5. 搜索页的条目链接**只在** ``<div class="result-item">`` 的 ``head`` 块里可靠，
   ``body`` 是正文预览，里面的 ``/class/NNN.html`` 链接不能当结果用（实测搜
   ``sodium`` 时第一条是「植物魔法」，只因正文提到过 Sodium）。搜索结果必须
   **classId + 标题一起取、并用标题校验**，校验不过就返回「没找到」。
"""

from __future__ import annotations

import html as html_module
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..cache import CacheManager
from ..jarinfo import JarInfo
from ..net import HttpClient, NetworkError, RateLimiter
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

__all__ = ["parse_run_env", "McModSource", "McModRecord"]

#: 搜索结果条目块的起始标记（``mold=1`` 时每条结果一个 ``result-item``）
_RESULT_ITEM_RE = re.compile(r'<div class="result-item"', re.IGNORECASE)
#: 条目块里「正文预览」的起点：标题在它之前的 ``head`` 块内，之后的链接一律不可信
_RESULT_BODY_MARK = 'class="body"'
#: ``head`` 块内（或全页面兜底时）的条目标题链接：href 指向 ``/class/NNN.html``，
#: 锚文本里可能还嵌着 ``<em>``（例如「钠 (<em>Sodium</em>)」），所以内层要允许标签
_ENTRY_LINK_RE = re.compile(
    r'<a\b[^>]*?href="[^"]*?/class/(\d+)\.html"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
#: 锚文本里的内嵌标签（``<em>``/``<span>``…），取标题时必须剥掉
_TAG_RE = re.compile(r"<[^>]*>")
#: ``head`` 块找不到 ``class="body"`` 时，最多往后看这么多字符
_HEAD_FALLBACK_CHARS = 1500
#: 「真的没有搜索结果」的页面上会写这句；mcmod 偶发的空壳页面**没有**它
_NO_RESULT_MARK = "没有找到"
#: 搜索页最多重新抓几次（用来扛 mcmod 偶发的空壳页面；仍是串行 + 限流）
_SEARCH_MAX_ATTEMPTS = 2

#: 详情页「运行环境」字段。先试「跳过任意多个标签」，再退回简单版；
#: 页面结构是 ``<span>运行环境：</span><span>客户端需装, 服务端无效</span>``
_RUN_ENV_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"运行环境\s*[:：]\s*(?:<[^>]*>\s*)*([^<\n]{2,40})"),
    re.compile(r"运行环境\s*[:：]\s*([^<\n]{2,40})"),
)

_WS_RE = re.compile(r"[\s\u3000]+")


def parse_run_env(text: Optional[str]) -> Tuple[str, str]:
    """把「运行环境」原文翻成 ``(side, 依据说明)``。

    **口径（项目所有者确认）：只有「无效」才算不需要；「需装」和「可选」一律视为需要。**

    代价不对等：多判一端只是多发一个 mod（浪费带宽），少判一端会让那一端缺 mod
    直接崩游戏。所以：

    * ``服务端无效``（且服务端没被标「需装/可选」）-> 纯客户端；
    * ``客户端无效``（且客户端没被标「需装/可选」）-> 纯服务端；
    * 两端都被标了「需装」或「可选」-> 双端；
    * 只有半边信息（如只写「客户端需装」）-> 保守判双端
      （HANDOFF 实测：``创世神`` 在 mcmod 上只写了「客户端需装」，而它其实是双端）。

    识别不到的文案返回 ``unknown``，交由上层降级到其它数据源。
    """
    raw = str(text or "").strip()
    if not raw:
        return SIDE_UNKNOWN, "运行环境字段缺失"
    compact = _WS_RE.sub("", html_module.unescape(raw))

    c_need = "客户端需装" in compact
    c_opt = "客户端可选" in compact
    c_bad = "客户端无效" in compact
    s_need = "服务端需装" in compact
    s_opt = "服务端可选" in compact
    s_bad = "服务端无效" in compact

    client_mentioned = c_need or c_opt or c_bad
    server_mentioned = s_need or s_opt or s_bad
    # 「需装」与「可选」都算**需要**（可选 = 可以装，不是不必装）
    client_needed = c_need or c_opt
    server_needed = s_need or s_opt

    if not (client_mentioned or server_mentioned):
        return SIDE_UNKNOWN, f"无法识别的运行环境文案「{raw}」"

    # 「无效」= 该端装了没用 -> 不需要。这是唯一能推出单侧结论的依据。
    if s_bad and not server_needed:
        return SIDE_CLIENT, f"运行环境「{raw}」-> 服务端无效"
    if c_bad and not client_needed:
        return SIDE_SERVER, f"运行环境「{raw}」-> 客户端无效"
    if client_needed and server_needed:
        return SIDE_BOTH, f"运行环境「{raw}」-> 两端都需要"
    if client_mentioned and server_mentioned:
        return SIDE_BOTH, f"运行环境「{raw}」文案组合异常，保守判双端"
    if client_needed or server_needed:
        # 只有半边信息（例如只写「客户端需装」）—— 绝不能凭这半句判单侧
        return SIDE_BOTH, f"运行环境「{raw}」只有单侧信息，保守判双端"
    return SIDE_BOTH, f"运行环境「{raw}」无有效侧别描述，保守判双端"


def _clean_entry_title(raw: str) -> str:
    """把锚文本洗成纯标题：剥标签 -> 反转义实体 -> 压空白 -> 截断。"""
    text = _TAG_RE.sub("", str(raw or ""))
    text = html_module.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:120]


def _parse_search_results_ex(page: str) -> Tuple[List[Tuple[int, str]], bool]:
    """解析搜索结果页，返回 ``(条目列表, 是否可信)``。

    * **可信**（页面里有 ``<div class="result-item">``）：只取每个条目 ``head``
      块里的标题链接，``body`` 正文里的链接一律忽略；
    * **不可信**（没有 ``result-item``，说明页面结构变了）：退回「全页面扫描
      ``class`` 链接」，这种结果调用方必须用更严的匹配规则校验后才可以采信。
    """
    text = str(page or "")
    entries: List[Tuple[int, str]] = []
    seen: set = set()

    starts = [match.start() for match in _RESULT_ITEM_RE.finditer(text)]
    if starts:
        for index, start in enumerate(starts):
            next_start = starts[index + 1] if index + 1 < len(starts) else len(text)
            body_at = text.find(_RESULT_BODY_MARK, start, next_start)
            end = body_at if body_at != -1 else min(next_start, start + _HEAD_FALLBACK_CHARS)
            head = text[start:end]
            found = _ENTRY_LINK_RE.search(head)
            if found is None:
                continue
            class_id = int(found.group(1))
            title = _clean_entry_title(found.group(2))
            if not title or class_id in seen:  # 没标题就无法校验，宁可丢掉
                continue
            seen.add(class_id)
            entries.append((class_id, title))
        return entries, True

    for found in _ENTRY_LINK_RE.finditer(text):
        class_id = int(found.group(1))
        title = _clean_entry_title(found.group(2))
        if not title or class_id in seen:
            continue
        seen.add(class_id)
        entries.append((class_id, title))
    return entries, False


def _parse_search_results(page: str) -> List[Tuple[int, str]]:
    """从搜索结果页提取 ``[(classId, 标题), ...]``，按页面顺序，去重。

    只认 ``<div class="result-item">`` 里 ``<div class="head">`` 块中的条目标题
    链接；``<div class="body">`` 正文里的链接必须忽略。

    .. warning::
       返回空列表可能是「真的没搜到」，也可能是「页面结构又变了」，两者这里
       分不出来 —— 需要区分时请用 :func:`_parse_search_results_ex`。
    """
    return _parse_search_results_ex(page)[0]


@dataclass
class McModRecord:
    """一次 mcmod 查询的完整留痕（便于人工复核与缓存）。"""

    query_name: str = ""
    class_id: int = 0
    title: str = ""
    run_env: str = ""
    side: str = SIDE_UNKNOWN
    detail: str = ""
    matched: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_name": self.query_name,
            "class_id": self.class_id,
            "title": self.title,
            "run_env": self.run_env,
            "side": self.side,
            "detail": self.detail,
            "matched": self.matched,
        }


@dataclass
class McModSource:
    """mcmod 查询（**串行 + 限流**，带缓存）。"""

    client: HttpClient
    cache: CacheManager
    base: str = "https://www.mcmod.cn"
    min_interval: float = 0.6
    enabled: bool = True
    #: 每个 jar 最多尝试几个候选名（越多越慢，每个候选都要网络请求）
    max_candidates: int = 3

    _limiter: RateLimiter = field(default=None, repr=False)
    errors: List[str] = field(default_factory=list, repr=False)
    _looked_up: int = field(default=0, repr=False)
    #: 本次扫描中真正发起过的网络请求数（用于报告与验收）
    requests_made: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self._limiter is None:
            self._limiter = RateLimiter(self.min_interval)

    # ------------------------------------------------------------ 单步请求
    def search_class_id(self, name: str) -> Tuple[Optional[int], str, str]:
        """搜索一个名字，返回 ``(classId, 页面标题, 错误)``。

        **只返回标题与 ``name`` 匹配上的条目**；一条都没匹配上时返回
        ``class_id=None`` —— 拿错条目的运行环境去判定，比没有结论危害大得多。

        「无结果」会被缓存为 ``0``（连同空标题），避免每次扫描都重搜
        （搜索是最大耗时项）。**但要先区分「真没收录」和「页面偶发空壳」**：
        mcmod 偶尔（实测约 15%）会返回一个没有结果列表、也没有「没有找到」
        文案的空壳页面，把它当成「搜不到」缓存 720 小时，会让这个 mod 长期
        被降级到别的数据源甚至错配条目。
        """
        key = str(name or "").strip()
        if not key:
            return None, "", "空名字"
        cached = self.cache.get_mcmod_class_id(key)
        if cached is not None:
            if cached <= 0:
                return None, "", "无结果（缓存）"
            cached_title = self.cache.get_mcmod_title(key)
            if cached_title:
                return cached, cached_title, ""
            # 老缓存只有 classId、没记标题 —— 无法校验来源，宁可重搜一次

        url = f"{self.base}/s?key={urllib.parse.quote(key)}&mold=1"
        entries: List[Tuple[int, str]] = []
        trusted = True
        for attempt in range(1, _SEARCH_MAX_ATTEMPTS + 1):
            self._limiter.wait()  # 串行 + 限流：重试同样要排队，不并发
            self.requests_made += 1
            try:
                page = self.client.get_text(url, encoding="utf-8")
            except NetworkError as exc:
                self.errors.append(f"mcmod 搜索「{key}」失败：{exc}")
                return None, "", f"搜索失败：{exc}"

            entries, trusted = _parse_search_results_ex(page)
            if entries:
                break
            if _NO_RESULT_MARK in page:
                # 页面上明写了「没有找到」—— 确实没收录这个条目
                self.cache.put_mcmod_class_id(key, 0, "")
                return None, "", "搜索无结果"
            # 没有结果列表、也没有「没有找到」—— mcmod 偶发的空壳页面
            if attempt < _SEARCH_MAX_ATTEMPTS:
                continue
            # 重试后仍是空壳：**不要缓存**，否则 720 小时内都不会再搜这个 mod
            return None, "", "搜索页面异常（无结果列表，未缓存）"

        for class_id, title in entries:
            if not title:
                continue
            # 兜底解析（页面结构变了）拿到的条目一律不可信：只认严格同名，
            # 否则宁可不采信。
            ok = _name_matches(key, title) if trusted else _exact_name_matches(key, title)
            if ok:
                self.cache.put_mcmod_class_id(key, class_id, title)
                return class_id, title, ""

        self.cache.put_mcmod_class_id(key, 0, "")
        return None, "", f"搜索无匹配条目（首条是「{entries[0][1]}」）"

    def fetch_run_env(self, class_id: int) -> Tuple[Optional[str], str]:
        """抓详情页的「运行环境」原文，返回 ``(原文, 错误)``。"""
        cached = self.cache.get_mcmod_env(class_id)
        if cached is not None:
            run_env = str(cached.get("run_env") or "")
            if run_env:
                return run_env, ""
            return None, str(cached.get("error") or "页面无该字段（缓存）")

        url = f"{self.base}/class/{int(class_id)}.html"
        self._limiter.wait()
        self.requests_made += 1
        try:
            page = self.client.get_text(url, encoding="utf-8")
        except NetworkError as exc:
            self.errors.append(f"mcmod 详情页 {class_id} 抓取失败：{exc}")
            return None, f"详情页抓取失败：{exc}"

        for pattern in _RUN_ENV_PATTERNS:
            match = pattern.search(page)
            if match is not None:
                value = html_module.unescape(match.group(1)).strip()
                if value:
                    self.cache.put_mcmod_env(class_id, {"run_env": value})
                    return value, ""
        self.cache.put_mcmod_env(class_id, {"run_env": "", "error": "页面无该字段"})
        return None, "页面无该字段"

    # ------------------------------------------------------------ 主流程
    def lookup(self, jar: JarInfo, progress: Optional[Any] = None) -> SideVerdict:
        """查一个 jar 的侧别。"""
        if not self.enabled:
            return SideVerdict.unknown("mcmod", "mcmod 数据源已关闭")
        record = self.lookup_record(jar, progress=progress)
        if record is None:
            return SideVerdict.unknown("mcmod", "无可用搜索名")
        if record.class_id <= 0:
            return SideVerdict.unknown("mcmod", record.detail)
        if record.side == SIDE_UNKNOWN:
            return SideVerdict.unknown("mcmod", record.detail)
        confidence = CONF_HIGH if record.matched else CONF_LOW
        return SideVerdict(
            side=record.side,
            confidence=confidence,
            source="mcmod",
            detail=record.detail + ("" if record.matched else "（条目名与 mod 名不完全一致，需人工确认）"),
            raw={
                "class_id": record.class_id,
                "title": record.title,
                "run_env": record.run_env,
                "query_name": record.query_name,
            },
        )

    def lookup_record(self, jar: JarInfo, progress: Optional[Any] = None) -> Optional[McModRecord]:
        """多策略搜索 + 抓运行环境，返回完整留痕（找不到返回 ``None`` 之外的记录）。"""
        names = jar.search_names()[: max(1, int(self.max_candidates))]
        if not names:
            return None

        last_error = ""
        for name in names:
            class_id, title, error = self.search_class_id(name)
            if class_id is None:
                last_error = error or "搜索无结果"
                continue
            # ``search_class_id`` 只会返回「标题与候选名匹配成功」的条目，
            # 所以这里恒为 True；字段保留只为在报告/留痕里标出「已验证过标题」，
            # 不再存在「标题为空就默认匹配」的分支。
            matched = True
            run_env, env_error = self.fetch_run_env(class_id)
            if run_env is None:
                # 字段缺失 ≠ 无侧别信息：如实返回 unknown，让上层继续降级到别的源。
                # （条目已经过标题校验，换候选名只会搜到同一条，没必要重试）
                last_error = f"class/{class_id} {env_error}"
                return McModRecord(
                    query_name=name,
                    class_id=class_id,
                    title=title,
                    run_env="",
                    side=SIDE_UNKNOWN,
                    detail=f"mcmod class/{class_id}（{title or '未知条目'}）：{env_error}",
                    matched=matched,
                )
            side, reason = parse_run_env(run_env)
            return McModRecord(
                query_name=name,
                class_id=class_id,
                title=title,
                run_env=run_env,
                side=side,
                detail=f"mcmod class/{class_id}（{title or name}）：{reason}",
                matched=matched,
            )

        return McModRecord(
            query_name=names[0],
            class_id=0,
            side=SIDE_UNKNOWN,
            detail=f"mcmod 未找到条目（尝试 {len(names)} 个名字；最后错误：{last_error or '无结果'}）",
        )

    def lookup_many(
        self,
        jars: Sequence[JarInfo],
        progress: Optional[Any] = None,
        should_stop: Optional[Any] = None,
    ) -> Dict[str, SideVerdict]:
        """串行查询一批 jar（**不能并发**，限流会失效）。"""
        result: Dict[str, SideVerdict] = {}
        total = len(jars)
        for index, jar in enumerate(jars, start=1):
            if should_stop is not None and should_stop():
                break
            verdict = self.lookup(jar)
            if verdict.known:
                result[jar.rel] = verdict
            self._looked_up += 1
            if progress is not None:
                progress(index, total, f"MC 百科：{jar.display_name or jar.name}")
        return result


def _name_matches(query: str, title: str) -> bool:
    """粗判「搜到的条目」是否就是我们要找的 mod。

    只做保守判断：规范化后互为子串即算匹配。判不出来时返回 ``True``
    （宁可采信并降置信度，也不要因为标题格式差异丢掉高价值数据源）。
    """
    left = _WS_RE.sub("", str(query or "")).lower()
    right = _WS_RE.sub("", str(title or "")).lower()
    if not left or not right:
        return True
    if left in right or right in left:
        return True
    # 去掉常见修饰后再比一次
    strip = re.compile(r"[（()）\[\]【】\-_:：·.]")
    left_core = strip.sub("", left)
    right_core = strip.sub("", right)
    if left_core and right_core and (left_core in right_core or right_core in left_core):
        return True
    return False


_DECOR_RE = re.compile(r"[（()）\[\]【】\-_:：·.]")
_PAREN_RE = re.compile(r"[（(\[【]([^）)\]】]{2,60})[）)\]】]")


def _normalize_name(value: str) -> str:
    """规范化名字：反转义 -> 小写 -> 去空白 -> 去括号/连字符等修饰符。"""
    text = html_module.unescape(str(value or "")).lower()
    text = _WS_RE.sub("", text)
    return _DECOR_RE.sub("", text)


def _exact_name_matches(query: str, title: str) -> bool:
    """严格匹配：规范化后必须**完全相等**（或等于标题括号里的那个名字）。

    只用于 :func:`_parse_search_results_ex` 里「页面结构变了、走了全页面兜底」
    这种**不可信**来源的条目 —— 那种情况下互为子串的粗判太容易误采信
    （实测：搜 ``sodium`` 时 ``[BOT] 植物魔法 (Botania)`` 的正文提到过 Sodium）。
    """
    left = _normalize_name(query)
    right = _normalize_name(title)
    if not left or not right:
        return False
    if left == right:
        return True
    for inner in _PAREN_RE.findall(str(title or "")):
        if _normalize_name(inner) == left:
            return True
    return False
