"""核心逻辑单元测试（标准库 unittest，无需第三方依赖）。

覆盖三条最容易出事的地方：

1. mcmod「运行环境」文案解析 —— 保守性（只有半边信息时必须判双端）；
2. 多源融合 —— **铁律：没有明确依据就绝不许判 `server`**；
3. ``side-report.json`` 的契约字段 —— AutoSync 靠它联动，少一个字段就白干。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cache import CacheManager, jar_cache_key
from src.detector import Detector, ModResult, ScanReport, merge_verdicts  # noqa: E402
from src.jarinfo import JarInfo, parse_fabric_json, parse_mod_toml  # noqa: E402
from src.report import side_report_payload  # noqa: E402
from src.sources.mcmod import parse_run_env  # noqa: E402
from src.verdict import (  # noqa: E402
    CONF_HIGH,
    CONF_LOW,
    SIDE_BOTH,
    SIDE_CLIENT,
    SIDE_SERVER,
    SIDE_UNKNOWN,
    SideVerdict,
)


def _jar(**kwargs) -> JarInfo:
    """造一个最小的 JarInfo（只用于融合测试）。"""
    jar = JarInfo(rel="test.jar", name="test.jar", size=1, sha1="a" * 40)
    for key, value in kwargs.items():
        setattr(jar, key, value)
    return jar


class TestParseRunEnv(unittest.TestCase):
    """mcmod「运行环境」-> side 的映射。"""

    def test_client_only(self) -> None:
        self.assertEqual(parse_run_env("客户端需装, 服务端无效")[0], SIDE_CLIENT)
        self.assertEqual(parse_run_env("客户端可选, 服务端无效")[0], SIDE_CLIENT)
    def test_both(self) -> None:
        self.assertEqual(parse_run_env("客户端需装, 服务端需装")[0], SIDE_BOTH)
        self.assertEqual(parse_run_env("客户端可选, 服务端可选")[0], SIDE_BOTH)
        self.assertEqual(parse_run_env("客户端可选, 服务端需装")[0], SIDE_BOTH)
        # 项目口径：「可选」也算要装（只有「无效」才算不需要）。
        # 所以「客户端需装 + 服务端可选」-> 双端，不是纯客户端。
        self.assertEqual(parse_run_env("客户端需装, 服务端可选")[0], SIDE_BOTH)

    def test_server_only(self) -> None:
        self.assertEqual(parse_run_env("服务端需装, 客户端无效")[0], SIDE_SERVER)

    def test_half_information_is_conservative(self) -> None:
        """只有半边信息（「客户端需装」）**绝不能**判单侧 —— 实测「创世神」
        在 mcmod 上就只写了这半句，而它其实是双端 mod。"""
        self.assertEqual(parse_run_env("客户端需装")[0], SIDE_BOTH)
        self.assertEqual(parse_run_env("服务端需装")[0], SIDE_BOTH)

    def test_unknown_text(self) -> None:
        self.assertEqual(parse_run_env("")[0], SIDE_UNKNOWN)
        self.assertEqual(parse_run_env("未知")[0], SIDE_UNKNOWN)

    def test_tolerates_whitespace_and_fullwidth(self) -> None:
        self.assertEqual(parse_run_env("客户端 需装 ， 服务端 无效")[0], SIDE_CLIENT)
        self.assertEqual(parse_run_env("运行环境：客户端需装, 服务端无效")[0], SIDE_CLIENT)


class TestMergeRules(unittest.TestCase):
    """多源融合规则与铁律。"""

    def test_two_sources_agree(self) -> None:
        verdicts = {
            "mcmod": SideVerdict(SIDE_CLIENT, CONF_HIGH, "mcmod", "x"),
            "modrinth": SideVerdict(SIDE_CLIENT, CONF_HIGH, "modrinth", "y"),
        }
        result = merge_verdicts(_jar(), verdicts, None)
        self.assertEqual(result.side, SIDE_CLIENT)
        self.assertEqual(result.confidence, CONF_HIGH)
        self.assertFalse(result.conflict)

    def test_two_sources_conflict_becomes_both(self) -> None:
        verdicts = {
            "mcmod": SideVerdict(SIDE_CLIENT, CONF_HIGH, "mcmod", "x"),
            "modrinth": SideVerdict(SIDE_BOTH, CONF_HIGH, "modrinth", "y"),
        }
        result = merge_verdicts(_jar(), verdicts, None)
        self.assertEqual(result.side, SIDE_BOTH)
        self.assertTrue(result.conflict)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.confidence, CONF_LOW)

    def test_single_source_is_medium(self) -> None:
        verdicts = {"modrinth": SideVerdict(SIDE_CLIENT, CONF_HIGH, "modrinth", "y")}
        result = merge_verdicts(_jar(), verdicts, None)
        self.assertEqual(result.side, SIDE_CLIENT)
        self.assertEqual(result.confidence, "medium")

    def test_no_source_defaults_to_both(self) -> None:
        result = merge_verdicts(_jar(), {}, None)
        self.assertEqual(result.side, SIDE_BOTH)
        self.assertTrue(result.needs_review)

    def test_never_judge_server_without_evidence(self) -> None:
        """**铁律**：低置信度的 server 判定必须被降级成 both。

        真实事故：被 18 个 mod 依赖的前置 ``sable`` 被误判成纯服务端并移出客户端
        目录，NeoForge 直接报 ``Missing or unsupported mandatory dependencies``。
        """
        verdicts = {"heuristics": SideVerdict(SIDE_SERVER, CONF_LOW, "heuristics", "猜的")}
        result = merge_verdicts(_jar(), verdicts, None)
        self.assertEqual(result.side, SIDE_BOTH)
        self.assertTrue(result.needs_review)

    def test_server_with_evidence_is_kept(self) -> None:
        """有明确依据（client_side=unsupported）时才允许判纯服务端。"""
        verdicts = {
            "mcmod": SideVerdict(SIDE_SERVER, CONF_HIGH, "mcmod", "客户端无效"),
            "modrinth": SideVerdict(SIDE_SERVER, CONF_HIGH, "modrinth", "client=unsupported"),
        }
        result = merge_verdicts(_jar(), verdicts, None)
        self.assertEqual(result.side, SIDE_SERVER)
        self.assertFalse(result.conflict)

    def test_manual_override_wins(self) -> None:
        verdicts = {
            "mcmod": SideVerdict(SIDE_BOTH, CONF_HIGH, "mcmod", "x"),
            "modrinth": SideVerdict(SIDE_BOTH, CONF_HIGH, "modrinth", "y"),
        }
        override = {"side": "client", "note": "服主确认", "at": "2026-01-01T00:00:00+0800"}
        result = merge_verdicts(_jar(), verdicts, override)
        self.assertEqual(result.side, SIDE_CLIENT)
        self.assertTrue(result.manual)
        self.assertFalse(result.needs_review)


class TestJarInfoParsing(unittest.TestCase):
    """TOML / Fabric 解析。"""

    def test_inline_comment_on_section_header(self) -> None:
        """``[[mods]] #mandatory`` 这种行内注释必须容忍（AutoSync 踩过的坑）。"""
        text = (
            'modLoader="javafml"\n'
            "[[mods]] #mandatory\n"
            'modId="sable"\n'
            'version="1.0.0"\n'
            'displayName="Sable"\n'
            'displayTest="IGNORE_ALL_VERSION"\n'
        )
        info = parse_mod_toml(text, "META-INF/neoforge.mods.toml")
        self.assertEqual(info.mod_ids, ["sable"])
        self.assertEqual(info.mods[0].display_name, "Sable")
        self.assertEqual(info.mods[0].display_test, "IGNORE_ALL_VERSION")

    def test_unicode_escape_in_display_name(self) -> None:
        text = '[[mods]]\nmodId="x"\ndisplayName="\\u94a0 (Sodium)"\n'
        info = parse_mod_toml(text)
        self.assertEqual(info.mods[0].display_name, "钠 (Sodium)")
        self.assertIn("钠", info.mods[0].search_names)
        self.assertIn("Sodium", info.mods[0].search_names)

    def test_client_side_only(self) -> None:
        text = '[[mods]]\nmodId="x"\nclientSideOnly=true\n'
        info = parse_mod_toml(text)
        self.assertIs(info.mods[0].client_side_only, True)

    def test_fabric_environment(self) -> None:
        client = parse_fabric_json(json.dumps({"id": "x", "name": "X", "environment": "client"}))
        self.assertEqual(client.mods[0].client_side_only, True)
        server = parse_fabric_json(json.dumps({"id": "y", "environment": "server"}))
        self.assertEqual(server.mods[0].client_side_only, False)
        both = parse_fabric_json(json.dumps({"id": "z", "environment": "*"}))
        self.assertIsNone(both.mods[0].client_side_only)


class TestSideReportContract(unittest.TestCase):
    """``side-report.json`` 必须满足 HANDOFF 4.2 节的契约。"""

    def _report(self) -> ScanReport:
        report = ScanReport(generated_at="2026-01-01T00:00:00+0800", mods_dir="D:/mods")
        report.mods = [
            ModResult(
                rel="a.jar",
                name="a.jar",
                sha1="b" * 40,
                sha256="c" * 64,
                mod_id="sodium",
                display_name="Sodium",
                side=SIDE_CLIENT,
                confidence=CONF_HIGH,
                sources={"modrinth": SideVerdict(SIDE_CLIENT, CONF_HIGH, "modrinth", "x").to_dict()},
                notes="ok",
            )
        ]
        return report

    def test_required_fields(self) -> None:
        payload = side_report_payload(self._report())
        self.assertIn("generated", payload)
        self.assertIn("tool", payload)
        self.assertTrue(payload["tool"].startswith("ModSideDetector"))
        self.assertEqual(len(payload["mods"]), 1)
        mod = payload["mods"][0]
        for key in (
            "file",
            "sha1",
            "sha256",
            "mod_id",
            "display_name",
            "side",
            "confidence",
            "sources",
            "notes",
        ):
            self.assertIn(key, mod, f"side-report.json 缺少契约字段 {key}")
        self.assertEqual(mod["side"], SIDE_CLIENT)
        self.assertIn(mod["confidence"], ("high", "medium", "low"))

    def test_json_serializable(self) -> None:
        json.dumps(side_report_payload(self._report()), ensure_ascii=False)


class TestCacheOverrides(unittest.TestCase):
    """人工复核缓存：一次确认，永久生效（且 sha1 优先于 modId）。"""

    def test_override_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "side-cache.json"
            cache = CacheManager.load(path, ttl_hours=720.0)
            cache.put_override("client", sha1="d" * 40, mod_id="sodium", note="人工")
            cache.save()

            reloaded = CacheManager.load(path, ttl_hours=720.0)
            hit = reloaded.get_override("d" * 40, "sodium")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["side"], "client")

    def test_jar_cache_key_changes_with_mtime(self) -> None:
        self.assertNotEqual(
            jar_cache_key("a.jar", 10, 100),
            jar_cache_key("a.jar", 10, 200),
        )

    def test_override_requires_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = CacheManager.load(Path(tmp) / "c.json")
            with self.assertRaises(ValueError):
                cache.put_override("client")


class TestDetectorEndToEnd(unittest.TestCase):
    """用真实 jar 跑一遍完整链路（不联网：两个网络源都关掉）。"""

    MODS_DIR = Path(r"C:\Users\一只屑\Desktop\ModSync\mods")

    @unittest.skipUnless(MODS_DIR.is_dir(), "真实测试数据集不存在，跳过")
    def test_offline_scan_never_judges_server(self) -> None:
        from src.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            config = Config(use_mcmod=False, use_modrinth=False, cache_dir=tmp)
            cache = CacheManager.load(Path(tmp) / "side-cache.json", ttl_hours=0)
            detector = Detector(config=config, cache=cache)
            report = detector.scan(self.MODS_DIR, limit=15)

        self.assertGreater(len(report.mods), 0)
        # 离线时没有主源数据，**任何一条都不允许是 server**
        offenders = [mod.name for mod in report.mods if mod.side == SIDE_SERVER]
        self.assertEqual(offenders, [], f"离线扫描出现了纯服务端判定：{offenders}")


class TestClassifyCopy(unittest.TestCase):
    """一键分类的安全约定：**默认只复制不移动**，同名不同内容**绝不覆盖**。"""

    def _setup(self, tmp: str) -> tuple[Path, ScanReport]:
        mods = Path(tmp) / "mods"
        mods.mkdir(parents=True, exist_ok=True)
        report = ScanReport(generated_at="2026-01-01T00:00:00+0800", mods_dir=str(mods))
        for name, side in (
            ("client-mod.jar", SIDE_CLIENT),
            ("server-mod.jar", SIDE_SERVER),
            ("both-mod.jar", SIDE_BOTH),
            ("unknown-mod.jar", SIDE_UNKNOWN),
        ):
            (mods / name).write_bytes(f"content of {name}".encode())
            report.mods.append(ModResult(rel=name, name=name, side=side))
        return mods, report

    def test_copy_to_matching_dirs(self) -> None:
        from src.report import classify_copy

        with tempfile.TemporaryDirectory() as tmp:
            mods, report = self._setup(tmp)
            out = Path(tmp) / "out"
            result = classify_copy(report, mods, out)

            self.assertTrue((out / "client-mods" / "client-mod.jar").is_file())
            self.assertTrue((out / "server-mods" / "server-mod.jar").is_file())
            self.assertTrue((out / "both-mods" / "both-mod.jar").is_file())
            # unknown 保守归入 both-mods（两边都要装）
            self.assertTrue((out / "both-mods" / "unknown-mod.jar").is_file())

    def test_source_files_are_never_moved(self) -> None:
        """红线：默认是复制，源文件必须原封不动。"""
        from src.report import classify_copy

        with tempfile.TemporaryDirectory() as tmp:
            mods, report = self._setup(tmp)
            classify_copy(report, mods, Path(tmp) / "out")
            for name in ("client-mod.jar", "server-mod.jar", "both-mod.jar", "unknown-mod.jar"):
                self.assertTrue((mods / name).is_file(), f"源文件 {name} 被移走了！")

    def test_never_overwrites_different_content(self) -> None:
        """目标已有同名但内容不同的文件时，记为冲突且**不覆盖**。"""
        from src.report import classify_copy

        with tempfile.TemporaryDirectory() as tmp:
            mods, report = self._setup(tmp)
            out = Path(tmp) / "out"
            target_dir = out / "client-mods"
            target_dir.mkdir(parents=True, exist_ok=True)
            precious = target_dir / "client-mod.jar"
            precious.write_bytes(b"IMPORTANT EXISTING CONTENT")

            result = classify_copy(report, mods, out)

            self.assertEqual(precious.read_bytes(), b"IMPORTANT EXISTING CONTENT")
            self.assertEqual(len(result.conflicts), 1)
            self.assertIn("client-mod.jar", result.conflicts[0])

    def test_skips_identical_target(self) -> None:
        from src.report import classify_copy

        with tempfile.TemporaryDirectory() as tmp:
            mods, report = self._setup(tmp)
            out = Path(tmp) / "out"
            target_dir = out / "client-mods"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "client-mod.jar").write_bytes((mods / "client-mod.jar").read_bytes())

            result = classify_copy(report, mods, out)
            self.assertEqual(result.conflicts, [])
            self.assertIn("client-mod.jar", result.skipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
