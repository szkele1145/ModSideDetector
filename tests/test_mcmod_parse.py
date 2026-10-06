"""``mcmod`` 搜索页解析的单测。

核心回归点：搜索页里 ``<div class="body">`` 正文提到的词会把**无关条目**顶到
结果里（实测搜 ``sodium`` 第一条是「植物魔法」），所以解析必须只认
``<div class="head">`` 里的条目标题链接，并且必须做标题校验。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.cache import CacheManager
from src.sources.mcmod import (
    McModSource,
    _exact_name_matches,
    _name_matches,
    _parse_search_results,
    _parse_search_results_ex,
    parse_run_env,
)

# 实测抓取 https://www.mcmod.cn/s?key=sodium&mold=1 的片段（已按需截短）
SODIUM_PAGE = """
<div class="search-result-list">
  <div class="result-item">
    <div class="head">
      <div class="class-category"><ul><li><a class="c_2" href="//www.mcmod.cn/class/category/2-1.html" target="_blank"></a></li></ul></div>
      <a  target="_blank" href="https://www.mcmod.cn/class/332.html">[BOT] 植物魔法 (Botania)</a>
    </div>
    <div class="body">……正文里顺手提到了一句 <em>Sodium</em>，还有 <a href="/class/9999.html">无关链接</a>……</div>
  </div>
  <div class="result-item">
    <div class="head">
      <div class="class-category"><ul><li><a class="c_24" href="//www.mcmod.cn/class/category/24-1.html" target="_blank"></a></li></ul></div>
      <a  target="_blank" href="https://www.mcmod.cn/class/2785.html">钠 (<em>Sodium</em>)</a>
    </div>
    <div class="body">正文……</div>
  </div>
  <div class="result-item">
    <div class="head"><a href="//www.mcmod.cn/class/3697.html">Iris Shaders</a></div>
    <div class="body">正文……</div>
  </div>
</div>
"""


class ParseSearchResultsTest(unittest.TestCase):
    def test_head_links_only_and_order(self) -> None:
        entries = _parse_search_results(SODIUM_PAGE)
        self.assertEqual(
            entries,
            [(332, "[BOT] 植物魔法 (Botania)"), (2785, "钠 (Sodium)"), (3697, "Iris Shaders")],
        )

    def test_body_links_ignored(self) -> None:
        ids = [class_id for class_id, _ in _parse_search_results(SODIUM_PAGE)]
        self.assertNotIn(9999, ids)
        self.assertNotIn(1, ids)  # category 链接（/class/category/2-1.html）

    def test_title_has_tags_stripped(self) -> None:
        # 标题里的 <em> 必须剥掉，否则「钠」这条会因为 '<' 而抓不到
        entries = dict(_parse_search_results(SODIUM_PAGE))
        self.assertEqual(entries[2785], "钠 (Sodium)")

    def test_first_entry_is_not_category(self) -> None:
        first_id, first_title = _parse_search_results(SODIUM_PAGE)[0]
        self.assertEqual(first_id, 332)
        self.assertFalse(_name_matches("sodium", first_title))

    def test_sodium_query_matches_second_entry(self) -> None:
        hits = [
            (class_id, title)
            for class_id, title in _parse_search_results(SODIUM_PAGE)
            if _name_matches("sodium", title)
        ]
        self.assertEqual(hits, [(2785, "钠 (Sodium)")])

    def test_empty_page(self) -> None:
        self.assertEqual(_parse_search_results(""), [])
        self.assertEqual(_parse_search_results("<html><body>没有结果</body></html>"), [])

    def test_page_without_result_item_is_untrusted(self) -> None:
        entries, trusted = _parse_search_results_ex(
            '<a href="https://www.mcmod.cn/class/123.html">Sodium</a>'
        )
        self.assertFalse(trusted)
        self.assertEqual(entries, [(123, "Sodium")])
        # 可信页面
        _, trusted2 = _parse_search_results_ex(SODIUM_PAGE)
        self.assertTrue(trusted2)


class MatchTest(unittest.TestCase):
    def test_coarse_match(self) -> None:
        self.assertTrue(_name_matches("sodium", "钠 (Sodium)"))
        self.assertTrue(_name_matches("Iris Shaders", "Iris Shaders"))
        self.assertFalse(_name_matches("sodium", "[BOT] 植物魔法 (Botania)"))

    def test_exact_match(self) -> None:
        self.assertTrue(_exact_name_matches("Sodium", "钠 (Sodium)"))
        self.assertTrue(_exact_name_matches("iris shaders", "Iris Shaders"))
        self.assertFalse(_exact_name_matches("sodium", "Sodium Extra"))
        self.assertFalse(_exact_name_matches("sodium", "[BOT] 植物魔法 (Botania)"))
        self.assertFalse(_exact_name_matches("", "Sodium"))


class _FakeClient:
    """只按顺序吐页面的假 HttpClient（记录调用次数）。"""

    def __init__(self, pages: list) -> None:
        self.pages = list(pages)
        self.calls = 0

    def get_text(self, url: str, encoding: str = "utf-8") -> str:
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


#: mcmod 偶发返回的空壳页：没有 result-item，也没有「没有找到」文案
EMPTY_SHELL_PAGE = "<html><body>百科搜索 全部 () 模组 (0) 整合包 (0)</body></html>"
#: 真的没有收录：页面上明写「没有找到」
NO_RESULT_PAGE = "<html><body>抱歉，没有找到与「xxx」相关的内容</body></html>"
#: 正常结果页（sodium，第一条是无关的植物魔法）
HIT_PAGE = SODIUM_PAGE


class SearchClassIdTest(unittest.TestCase):
    """``search_class_id`` 的匹配校验 + 空壳页处理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = CacheManager.load(Path(self._tmp.name) / "c.json", ttl_hours=720.0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, pages: list) -> tuple:
        client = _FakeClient(pages)
        source = McModSource(client=client, cache=self.cache, min_interval=0.0)
        return source, client

    def test_returns_matched_entry_not_first(self) -> None:
        source, _ = self._source([HIT_PAGE])
        class_id, title, error = source.search_class_id("sodium")
        self.assertEqual((class_id, error), (2785, ""))
        self.assertEqual(title, "钠 (Sodium)")
        # 缓存里记的是校验过的条目
        self.assertEqual(self.cache.get_mcmod_class_id("sodium"), 2785)
        self.assertEqual(self.cache.get_mcmod_title("sodium"), "钠 (Sodium)")

    def test_no_match_returns_not_found_and_caches_zero(self) -> None:
        source, client = self._source([HIT_PAGE])
        class_id, title, error = source.search_class_id("完全没有的模组名")
        self.assertIsNone(class_id)
        self.assertIn("搜索无匹配条目", error)
        self.assertIn("植物魔法", error)  # 首条标题进错误信息，便于排查
        self.assertEqual(self.cache.get_mcmod_class_id("完全没有的模组名"), 0)
        self.assertEqual(client.calls, 1)

    def test_cached_hit_skips_network(self) -> None:
        source, client = self._source([HIT_PAGE])
        source.search_class_id("sodium")
        again = source.search_class_id("sodium")
        self.assertEqual(again, (2785, "钠 (Sodium)", ""))
        self.assertEqual(client.calls, 1)

    def test_true_no_result_is_cached(self) -> None:
        source, client = self._source([NO_RESULT_PAGE])
        class_id, _, error = source.search_class_id("某某")
        self.assertIsNone(class_id)
        self.assertEqual(error, "搜索无结果")
        self.assertEqual(self.cache.get_mcmod_class_id("某某"), 0)
        self.assertEqual(client.calls, 1)

    def test_empty_shell_is_retried(self) -> None:
        source, client = self._source([EMPTY_SHELL_PAGE, HIT_PAGE])
        class_id, title, _ = source.search_class_id("sodium")
        self.assertEqual((class_id, title), (2785, "钠 (Sodium)"))
        self.assertEqual(client.calls, 2)

    def test_empty_shell_twice_is_not_cached(self) -> None:
        source, client = self._source([EMPTY_SHELL_PAGE])
        class_id, _, error = source.search_class_id("sodium")
        self.assertIsNone(class_id)
        self.assertIn("页面异常", error)
        self.assertEqual(client.calls, 2)
        # 关键：不能把偶发空壳记成「搜不到」，否则 TTL 内再也不会重搜
        self.assertIsNone(self.cache.get_mcmod_class_id("sodium"))


class RunEnvTest(unittest.TestCase):
    """``parse_run_env`` 的文案映射。

    口径（项目所有者确认）：**只有「无效」才算不需要；「需装」和「可选」一律视为需要。**
    所以「客户端需装 + 服务端可选」是 both —— 服务端「可选」意味着可以装，而不是不必装；
    多装一个只是浪费带宽，少装一个会崩游戏。
    """

    def test_known_cases(self) -> None:
        self.assertEqual(parse_run_env("客户端需装, 服务端无效")[0], "client")
        self.assertEqual(parse_run_env("客户端可选, 服务端无效")[0], "client")
        self.assertEqual(parse_run_env("服务端需装, 客户端无效")[0], "server")
        self.assertEqual(parse_run_env("客户端需装, 服务端需装")[0], "both")
        self.assertEqual(parse_run_env("客户端需装, 服务端可选")[0], "both")
        self.assertEqual(parse_run_env("客户端可选, 服务端需装")[0], "both")
        self.assertEqual(parse_run_env("客户端可选, 服务端可选")[0], "both")
        self.assertEqual(parse_run_env("客户端需装")[0], "both")
        self.assertEqual(parse_run_env("")[0], "unknown")


if __name__ == "__main__":
    unittest.main()
