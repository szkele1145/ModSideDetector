"""jar 元数据解析：TOML / Fabric JSON / JiJ 递归 / 侧别特征 / 哈希。

解析思路取自 AutoSync 的 ``autosync/deps.py``（TOML 极简解析 + JiJ 嵌套递归），
按本项目需要做了扩展：

* ``[[mods]]`` 段除 ``modId`` / ``version`` 外，还保留 ``displayName`` /
  ``description`` / ``displayTest`` / ``clientSideOnly`` —— 前两个是 mcmod
  搜索的输入，后两个是弱信号；
* 额外支持 Fabric 的 ``fabric.mod.json``（``environment`` 字段比 Forge 的
  ``displayTest`` 可靠得多，虽然本项目主要面向 NeoForge）；
* 附带轻量侧别特征：mixin 配置里的 ``client`` / ``server`` 段、是否存在
  ``assets/`` / ``data/``。

**只读需要的 zip entry，绝不整包解压** —— 真实数据集单个 jar 可达上百 MB。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "TOML_CANDIDATES",
    "FABRIC_CANDIDATES",
    "JIJ_DIR_PREFIXES",
    "JIJ_METADATA_ENTRY",
    "JIJ_MAX_DEPTH",
    "JIJ_MAX_ENTRY_BYTES",
    "ModEntry",
    "ModTomlInfo",
    "NestedModRef",
    "JarInfo",
    "hash_file",
    "read_mod_toml",
    "parse_mod_toml",
    "parse_fabric_json",
    "scan_jar",
    "inspect_jar",
    "iter_jars",
]

#: jar 内可能的 Forge/NeoForge 模组元数据文件（NeoForge 优先，Forge 兜底）
TOML_CANDIDATES: Tuple[str, ...] = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")

#: Fabric 元数据（次要目标，但 ``environment`` 字段质量很高）
FABRIC_CANDIDATES: Tuple[str, ...] = ("fabric.mod.json",)

#: JiJ（Jar-in-Jar）嵌套 jar 所在目录。NeoForge 官方约定 ``META-INF/jarjar/``，
#: 但实测相当多 mod 放在 ``META-INF/jars/``；两处都要扫，否则会把嵌套提供的前置
#: 误判成「缺失」。
JIJ_DIR_PREFIXES: Tuple[str, ...] = ("META-INF/jarjar/", "META-INF/jars/")
JIJ_METADATA_ENTRY = "META-INF/jarjar/metadata.json"
#: 嵌套递归最大层数（防 zip 炸弹 / 异常数据）
JIJ_MAX_DEPTH = 3
#: 单个嵌套 jar 允许读入内存的上限，超过则跳过（避免被超大嵌套拖死）
JIJ_MAX_ENTRY_BYTES = 64 * 1024 * 1024
#: 哈希分块大小
_HASH_CHUNK = 1 << 20

# --- TOML 极简解析用的正则 -------------------------------------------------
# 段头允许行尾注释：`[[mods]] #mandatory` / `[[dependencies.sable]]  # 前置`
_TOML_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[\s*([^\[\]]+?)\s*\]\]\s*(?:#.*)?$")
_TOML_TABLE_RE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_TOML_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.*)$")

# --- 顶层 @Mod 注解（Forge 1.12 风格的老 mod 没有 mods.toml）----------------
_MIXIN_CONFIG_SUFFIX = ".mixins.json"


# ---------------------------------------------------------------- 工具
def hash_file(path: Path, chunk_size: int = _HASH_CHUNK) -> Tuple[str, str]:
    """流式计算哈希，返回 ``(sha1, sha256)``。

    注意与 AutoSync 的 ``hash_file`` 顺序相反（那边返回 ``(sha256, sha1)``），
    本项目统一成 ``(sha1, sha256)``，因为 Modrinth 用 sha1 查版本。
    """
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def iter_jars(directory: Path) -> List[Path]:
    """列出目录下（含子目录）的所有 ``.jar``，按路径排序，结果稳定可复现。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".jar"),
        key=lambda p: str(p.relative_to(root)).lower(),
    )


# ---------------------------------------------------------------- TOML 读取
def _read_archive_toml(archive: zipfile.ZipFile) -> Tuple[Optional[str], str]:
    """从**已打开**的 zip 里读模组元数据 TOML，返回 ``(文本, 来源 entry)``。

    按名读取需要的 entry，绝不整包解压：真实数据集单个 jar 可达上百 MB。
    """
    try:
        names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return None, ""
    for candidate in TOML_CANDIDATES:
        if candidate in names:
            try:
                return archive.read(candidate).decode("utf-8", "replace"), candidate
            except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError):
                return None, ""
    return None, ""


def read_mod_toml(jar_path: Path) -> Tuple[Optional[str], str]:
    """读出 jar 内的 Forge/NeoForge 元数据 TOML；读不到返回 ``(None, "")``。"""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            return _read_archive_toml(archive)
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, EOFError, RuntimeError):
        return None, ""


# ---------------------------------------------------------------- TOML 解析
def _unescape(body: str) -> str:
    """还原 TOML 基本字符串里的转义（含 ``\\uXXXX`` —— 中文 displayName 常见）。"""
    simple = {
        "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
        '"': '"', "\\": "\\",
    }
    out: List[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1]
        if nxt in simple:
            out.append(simple[nxt])
            index += 2
            continue
        if nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            raw = body[index + 2 : index + 2 + width]
            if len(raw) == width:
                try:
                    out.append(chr(int(raw, 16)))
                    index += 2 + width
                    continue
                except ValueError:
                    pass
        out.append(nxt)
        index += 2
    return "".join(out)


def _toml_value(raw: str) -> str:
    """取等号右边的值，去引号、去行尾注释（``modId="sable" #注释``）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    quote = text[0]
    if quote in "\"'":
        if text.startswith(quote * 3):  # 多行字符串 """...""" / '''...'''
            end = text.find(quote * 3, 3)
            body = text[3:end] if end >= 0 else text[3:]
        else:
            chars: List[str] = []
            index = 1
            while index < len(text):
                char = text[index]
                if quote == '"' and char == "\\" and index + 1 < len(text):
                    chars.append(text[index : index + 2])
                    index += 2
                    continue
                if char == quote:
                    break
                chars.append(char)
                index += 1
            body = "".join(chars)
        return _unescape(body) if quote == '"' else body
    hash_index = text.find("#")
    if hash_index >= 0:
        text = text[:hash_index]
    return text.strip()


def _normalize_section(name: str) -> Tuple[str, str]:
    """把段名拆成 ``(头部, 尾部)``：``dependencies."sable"`` -> ``("dependencies", "sable")``。"""
    parts = str(name or "").split(".", 1)
    head = _toml_value(parts[0]).strip().lower()
    tail = _toml_value(parts[1]).strip() if len(parts) > 1 else ""
    return head, tail


@dataclass
class ModEntry:
    """``[[mods]]`` 里的一项。"""

    mod_id: str = ""
    version: str = ""
    display_name: str = ""
    description: str = ""
    #: 原始 ``displayTest`` 值（未设置时为空字符串）
    display_test: str = ""
    #: ``clientSideOnly = true`` 时明确是纯客户端；False/未设置时为 None
    client_side_only: Optional[bool] = None
    #: 其余原始键值，便于排查
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def search_names(self) -> List[str]:
        """用于 mcmod 搜索的候选名，按可信度排序（去重、去空、保序）。

        ``displayName`` 常见形式是「中文名 (English Name)」或单纯的英文名，
        所以依次尝试：原文 → 括号内 → 括号外 → modId。
        """
        candidates: List[str] = []
        display = str(self.display_name or "").strip()
        if display:
            candidates.append(display)
            for group in re.findall(r"[（(\[【]([^）)\]】]{2,60})[）)\]】]", display):
                candidates.append(group.strip())
            stripped = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", " ", display).strip()
            if stripped and stripped != display:
                candidates.append(stripped)
        if self.mod_id:
            candidates.append(self.mod_id)
        result: List[str] = []
        for item in candidates:
            item = item.strip()
            if item and item not in result and item.lower() not in ("${mod_name}", "examplemod"):
                result.append(item)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "display_test": self.display_test,
            "client_side_only": self.client_side_only,
        }


@dataclass
class ModTomlInfo:
    """一个 jar 的 Forge/NeoForge 元数据解析结果。"""

    source: str = ""
    mods: List[ModEntry] = field(default_factory=list)
    #: ``[[dependencies.<owner>]]`` 原文（本项目只用来展示，不参与侧别判定）
    dependencies: List[Dict[str, str]] = field(default_factory=list)

    @property
    def mod_ids(self) -> List[str]:
        return [entry.mod_id for entry in self.mods if entry.mod_id]

    def primary(self) -> Optional[ModEntry]:
        return self.mods[0] if self.mods else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "mods": [entry.to_dict() for entry in self.mods],
            "dependencies": [dict(item) for item in self.dependencies],
        }


def parse_mod_toml(text: Optional[str], source: str = "") -> ModTomlInfo:
    """解析 ``neoforge.mods.toml`` / ``mods.toml``。

    只做逐行解析，不实现完整 TOML —— 这两个文件结构非常固定，引入第三方库
    反而违背「纯标准库」的约束。

    **坑**：``[[mods]]`` 段头后面可能跟行内注释（``[[mods]] #mandatory``），
    段头正则必须容忍行尾注释，否则会漏掉该 mod 的 modId（AutoSync 踩过）。
    """
    info = ModTomlInfo(source=source)
    if not text:
        return info

    section = ""
    owner = ""
    pending_mod: Optional[Dict[str, str]] = None
    pending_dep: Optional[Dict[str, str]] = None

    def flush_mod() -> None:
        nonlocal pending_mod
        if pending_mod is not None:
            mod_id = str(pending_mod.get("modId") or "").strip()
            if mod_id:
                raw_side_only = str(pending_mod.get("clientSideOnly") or "").strip().lower()
                client_side_only: Optional[bool] = None
                if raw_side_only in ("true", "false"):
                    client_side_only = raw_side_only == "true"
                info.mods.append(
                    ModEntry(
                        mod_id=mod_id,
                        version=str(pending_mod.get("version") or "").strip(),
                        display_name=str(pending_mod.get("displayName") or "").strip(),
                        description=str(pending_mod.get("description") or "").strip(),
                        display_test=str(pending_mod.get("displayTest") or "").strip(),
                        client_side_only=client_side_only,
                        extra=dict(pending_mod),
                    )
                )
        pending_mod = None

    def flush_dep() -> None:
        nonlocal pending_dep
        if pending_dep is not None:
            mod_id = str(pending_dep.get("modId") or "").strip()
            if mod_id:
                info.dependencies.append(
                    {
                        "mod_id": mod_id,
                        "version_range": str(pending_dep.get("versionRange") or "").strip(),
                        "type": str(pending_dep.get("type") or "").strip(),
                        "side": str(pending_dep.get("side") or "").strip(),
                        "owner": str(pending_dep.get("owner") or owner),
                    }
                )
        pending_dep = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _TOML_ARRAY_TABLE_RE.match(raw_line)
        if match is not None:
            flush_dep()
            flush_mod()
            head, tail = _normalize_section(match.group(1))
            if head == "mods":
                section = "mods"
                pending_mod = {}
            elif head == "dependencies":
                section = "dependencies"
                owner = tail
                pending_dep = {"owner": tail}
            else:
                section = "other"
            continue

        match = _TOML_TABLE_RE.match(raw_line)
        if match is not None:
            # 单层表会结束「数组表」上下文
            flush_dep()
            flush_mod()
            section = "other"
            continue

        match = _TOML_KV_RE.match(raw_line)
        if match is None:
            continue
        key = match.group(1)
        value = _toml_value(match.group(2))
        if section == "mods" and pending_mod is not None:
            pending_mod[key] = value
        elif section == "dependencies" and pending_dep is not None:
            pending_dep[key] = value

    flush_dep()
    flush_mod()
    return info


# ---------------------------------------------------------------- Fabric
def parse_fabric_json(text: Optional[str]) -> ModTomlInfo:
    """解析 ``fabric.mod.json``（``environment`` 字段直接给出侧别）。"""
    info = ModTomlInfo(source=FABRIC_CANDIDATES[0])
    if not text:
        return info
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return info
    if not isinstance(data, dict):
        return info

    environment = str(data.get("environment") or "*").strip().lower()
    display_name = ""
    for key in ("name", "id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            display_name = value.strip()
            break
    # environment: client=纯客户端；server=纯服务端；*=双端
    client_side_only: Optional[bool] = None
    if environment == "client":
        client_side_only = True
    elif environment == "server":
        client_side_only = False
    entry = ModEntry(
        mod_id=str(data.get("id") or "").strip(),
        version=str(data.get("version") or "").strip(),
        display_name=display_name,
        description=str(data.get("description") or "").strip(),
        client_side_only=client_side_only,
        extra={"environment": environment},
    )
    if entry.mod_id:
        info.mods.append(entry)
    return info


# ---------------------------------------------------------------- JiJ（嵌套 jar）
@dataclass
class NestedModRef:
    """一个由 JiJ 嵌套 jar 提供的 modId（不需要单独安装）。"""

    mod_id: str
    version: str = ""
    parent_rel: str = ""
    parent_mod_id: str = ""
    jar_entry: str = ""
    depth: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version": self.version,
            "parent_file": self.parent_rel,
            "parent_mod_id": self.parent_mod_id,
            "jar_entry": self.jar_entry,
            "depth": self.depth,
        }


@dataclass
class JarScanResult:
    """单个 jar 的扫描结果（自身元数据 + 它通过 JiJ 提供的 modId）。"""

    rel: str = ""
    info: Optional[ModTomlInfo] = None
    source: str = ""
    nested: List[NestedModRef] = field(default_factory=list)
    #: ``父jar!/嵌套entry`` -> 失败原因
    problems: Dict[str, str] = field(default_factory=dict)
    #: 无模组元数据的嵌套 jar 个数（普通库 jar，属正常现象）
    plain_libs: int = 0
    #: 顶层读不出 modId 的原因
    reason: str = ""

    @property
    def mod_ids(self) -> List[str]:
        return list(self.info.mod_ids) if self.info is not None else []

    @property
    def jij_mod_ids(self) -> List[str]:
        seen: List[str] = []
        for ref in self.nested:
            if ref.mod_id not in seen:
                seen.append(ref.mod_id)
        return seen


def _jij_entries(archive: zipfile.ZipFile, names: List[str]) -> Tuple[List[str], List[str]]:
    """找出归档里的嵌套 jar entry，返回 ``(entry 列表, 说明性备注)``。

    优先信 ``META-INF/jarjar/metadata.json``，缺失/损坏时退回「扫描 JiJ 目录下
    所有 .jar」；两类取**并集** —— 实测存在 metadata.json 列不全的包，漏一个
    就会把它提供的 modId 误报成缺失。
    """
    notes: List[str] = []
    found: List[str] = []
    seen: Set[str] = set()
    name_set = set(names)

    if JIJ_METADATA_ENTRY in name_set:
        payload: Any = None
        try:
            payload = json.loads(archive.read(JIJ_METADATA_ENTRY).decode("utf-8", "replace"))
        except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError) as exc:
            notes.append(f"{JIJ_METADATA_ENTRY} 读取/解析失败（{exc!r}），已按目录扫描兜底")
        if isinstance(payload, dict):
            entries = payload.get("jars")
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip().lstrip("/")
                    if not path:
                        continue
                    if path not in name_set:
                        notes.append(f"metadata.json 指向的 {path} 在 jar 内不存在，已跳过")
                        continue
                    if path not in seen:
                        seen.add(path)
                        found.append(path)
            elif payload is not None:
                notes.append("metadata.json 里没有 jars 列表，已按目录扫描兜底")

    for name in names:
        if not name.lower().endswith(".jar"):
            continue
        if not any(name.startswith(prefix) for prefix in JIJ_DIR_PREFIXES):
            continue
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found, notes


def _collect_nested(
    archive: zipfile.ZipFile,
    parent_rel: str,
    parent_mod_id: str,
    depth: int,
    visited: Set[str],
    out: List[NestedModRef],
    problems: Dict[str, str],
    counters: Dict[str, int],
) -> None:
    """递归收集 ``archive`` 内 JiJ 嵌套 jar 提供的 modId（有限深度 + 防环）。"""
    if depth > JIJ_MAX_DEPTH:
        return
    try:
        names = list(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return

    entries, notes = _jij_entries(archive, names)
    for note in notes:
        problems.setdefault(f"{parent_rel}!/{JIJ_METADATA_ENTRY}", note)

    for entry in entries:
        if entry in visited:
            continue  # 防环
        visited.add(entry)
        label = f"{parent_rel}!/{entry}"
        try:
            info_obj = archive.getinfo(entry)
            if info_obj.file_size > JIJ_MAX_ENTRY_BYTES:
                problems[label] = f"嵌套 jar 过大（{info_obj.file_size} 字节），已跳过"
                continue
            raw = archive.read(entry)
        except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError) as exc:
            problems[label] = f"嵌套 jar 读取失败：{exc!r}"
            continue
        try:
            inner = zipfile.ZipFile(io.BytesIO(raw))
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
            problems[label] = f"嵌套 jar 损坏：{exc!r}"
            continue
        with inner:
            text, source = _read_archive_toml(inner)
            if text is None:
                # 普通库 jar（caffeine、exp4j 之流）本来就没有 mods.toml，属正常现象
                counters["plain_libs"] = counters.get("plain_libs", 0) + 1
            else:
                info = parse_mod_toml(text, source)
                if not info.mod_ids:
                    problems.setdefault(label, f"{source} 里没有解析出 [[mods]] modId")
                for entry_mod in info.mods:
                    out.append(
                        NestedModRef(
                            mod_id=entry_mod.mod_id,
                            version=entry_mod.version,
                            parent_rel=parent_rel,
                            parent_mod_id=parent_mod_id,
                            jar_entry=entry,
                            depth=depth,
                        )
                    )
            _collect_nested(inner, parent_rel, parent_mod_id, depth + 1, visited, out, problems, counters)


def scan_jar(abs_path: Path, rel: str = "") -> JarScanResult:
    """扫描一个顶层 jar：自身 modId + 它通过 JiJ 提供的所有 modId。

    嵌套损坏、metadata 缺失或格式异常都只记进 ``problems``，不抛异常。
    """
    result = JarScanResult(rel=rel or Path(abs_path).name)
    try:
        with zipfile.ZipFile(abs_path) as archive:
            names: List[str] = []
            try:
                names = list(archive.namelist())
            except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
                names = []

            text, source = _read_archive_toml(archive)
            if text is None:
                fabric_text, fabric_source = _read_archive_entry(archive, FABRIC_CANDIDATES, names)
                if fabric_text is not None:
                    info = parse_fabric_json(fabric_text)
                    result.source = fabric_source
                    result.info = info
                    if not info.mod_ids:
                        result.reason = f"{fabric_source} 里没有解析出 id"
                else:
                    result.reason = "读不到 META-INF/neoforge.mods.toml / META-INF/mods.toml / fabric.mod.json"
            else:
                result.source = source
                result.info = parse_mod_toml(text, source)
                if not result.info.mod_ids:
                    result.reason = f"{source} 里没有解析出 [[mods]] modId"

            parent_mod_id = result.mod_ids[0] if result.mod_ids else ""
            counters: Dict[str, int] = {}
            _collect_nested(
                archive, result.rel, parent_mod_id, 1, set(), result.nested, result.problems, counters
            )
            result.plain_libs = counters.get("plain_libs", 0)
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, EOFError, RuntimeError) as exc:
        result.reason = f"jar 无法读取：{exc!r}"
    return result


def _read_archive_entry(
    archive: zipfile.ZipFile, candidates: Sequence[str], names: Sequence[str]
) -> Tuple[Optional[str], str]:
    """按候选名读一个文本 entry（``names`` 可预先算好以避免重复列目录）。"""
    name_set = set(names)
    for candidate in candidates:
        if candidate in name_set:
            try:
                return archive.read(candidate).decode("utf-8", "replace"), candidate
            except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError):
                return None, ""
    return None, ""


# ---------------------------------------------------------------- 完整 jar 信息
@dataclass
class JarInfo:
    """一个 jar 的完整可判定信息（元数据 + 侧别特征 + 哈希）。"""

    path: str = ""
    rel: str = ""
    name: str = ""
    size: int = 0
    mtime_ns: int = 0
    sha1: str = ""
    sha256: str = ""

    mod_ids: List[str] = field(default_factory=list)
    jij_mod_ids: List[str] = field(default_factory=list)
    display_name: str = ""
    version: str = ""
    description: str = ""
    toml_source: str = ""

    display_test: str = ""
    #: 来自 ``clientSideOnly`` / Fabric ``environment`` 的明确判定（None = 未知）
    toml_side: Optional[str] = None
    toml_side_detail: str = ""

    has_client_mixin: bool = False
    has_server_mixin: bool = False
    mixin_configs: List[str] = field(default_factory=list)
    has_assets: bool = False
    has_data: bool = False

    #: 顶层读不出 modId 的原因（非空 = 该 jar 无法参与判定）
    parse_error: str = ""
    problems: List[str] = field(default_factory=list)

    @property
    def primary_mod_id(self) -> str:
        return self.mod_ids[0] if self.mod_ids else ""

    @property
    def is_jar(self) -> bool:
        return self.rel.lower().endswith(".jar")

    def search_names(self) -> List[str]:
        """mcmod 搜索候选名（来自元数据，外加去版本号的文件名兜底）。"""
        candidates: List[str] = []
        if self.display_name:
            candidates.append(self.display_name)
            for group in re.findall(r"[（(\[【]([^）)\]】]{2,60})[）)\]】]", self.display_name):
                candidates.append(group.strip())
            stripped = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", " ", self.display_name).strip()
            if stripped and stripped != self.display_name:
                candidates.append(stripped)
        if self.primary_mod_id:
            candidates.append(self.primary_mod_id)
        fallback = re.sub(r"[-_]?\d[\w.\-+]*$", "", Path(self.name).stem).strip("-_ ")
        if fallback:
            candidates.append(fallback)
        result: List[str] = []
        for item in candidates:
            item = str(item or "").strip()
            if item and item not in result and item.lower() not in ("${mod_name}", "examplemod"):
                result.append(item)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rel": self.rel,
            "name": self.name,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha1": self.sha1,
            "sha256": self.sha256,
            "mod_ids": list(self.mod_ids),
            "jij_mod_ids": list(self.jij_mod_ids),
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "toml_source": self.toml_source,
            "display_test": self.display_test,
            "toml_side": self.toml_side,
            "toml_side_detail": self.toml_side_detail,
            "has_client_mixin": self.has_client_mixin,
            "has_server_mixin": self.has_server_mixin,
            "mixin_configs": list(self.mixin_configs),
            "has_assets": self.has_assets,
            "has_data": self.has_data,
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: Optional[Path] = None, rel: str = "") -> "JarInfo":
        """从缓存恢复（``path`` 用当前实际路径，避免盘符变动导致失效）。"""
        info = cls(
            path=str(path or data.get("path") or ""),
            rel=str(data.get("rel") or rel),
            name=str(data.get("name") or ""),
            size=int(data.get("size") or 0),
            mtime_ns=int(data.get("mtime_ns") or 0),
            sha1=str(data.get("sha1") or ""),
            sha256=str(data.get("sha256") or ""),
            display_name=str(data.get("display_name") or ""),
            version=str(data.get("version") or ""),
            description=str(data.get("description") or ""),
            toml_source=str(data.get("toml_source") or ""),
            display_test=str(data.get("display_test") or ""),
            toml_side=data.get("toml_side") or None,
            toml_side_detail=str(data.get("toml_side_detail") or ""),
            has_client_mixin=bool(data.get("has_client_mixin")),
            has_server_mixin=bool(data.get("has_server_mixin")),
            has_assets=bool(data.get("has_assets")),
            has_data=bool(data.get("has_data")),
            parse_error=str(data.get("parse_error") or ""),
        )
        info.mod_ids = [str(x) for x in (data.get("mod_ids") or [])]
        info.jij_mod_ids = [str(x) for x in (data.get("jij_mod_ids") or [])]
        info.mixin_configs = [str(x) for x in (data.get("mixin_configs") or [])]
        return info


def _inspect_mixins(archive: zipfile.ZipFile, names: Sequence[str]) -> Tuple[List[str], bool, bool]:
    """读所有 ``*.mixins.json``，返回 ``(配置文件列表, 有 client 段, 有 server 段)``。"""
    configs: List[str] = []
    has_client = False
    has_server = False
    for name in names:
        if not name.lower().endswith(_MIXIN_CONFIG_SUFFIX):
            continue
        try:
            data = json.loads(archive.read(name).decode("utf-8", "replace"))
        except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError):
            continue
        configs.append(name)
        if isinstance(data, dict):
            client = data.get("client")
            server = data.get("server")
            if isinstance(client, list) and client:
                has_client = True
            if isinstance(server, list) and server:
                has_server = True
    return configs, has_client, has_server


def inspect_jar(path: Path, rel: str = "", with_hash: bool = True) -> JarInfo:
    """把一个 jar 的判定依据全部读出来（元数据、弱信号、哈希）。"""
    path = Path(path)
    info = JarInfo(path=str(path), rel=rel or path.name, name=path.name)
    try:
        stat = path.stat()
        info.size = stat.st_size
        info.mtime_ns = stat.st_mtime_ns
    except OSError as exc:
        info.parse_error = f"无法读取文件属性：{exc!r}"
        return info

    if with_hash:
        try:
            info.sha1, info.sha256 = hash_file(path)
        except OSError as exc:
            info.parse_error = f"无法读取文件内容：{exc!r}"
            return info

    try:
        with zipfile.ZipFile(path) as archive:
            try:
                names = list(archive.namelist())
            except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as exc:
                info.parse_error = f"jar 目录列表读取失败：{exc!r}"
                return info

            info.mixin_configs, info.has_client_mixin, info.has_server_mixin = _inspect_mixins(archive, names)
            info.has_assets = any(name.startswith("assets/") for name in names)
            info.has_data = any(name.startswith("data/") for name in names)

            text, source = _read_archive_toml(archive)
            if text is None:
                fabric_text, fabric_source = _read_archive_entry(archive, FABRIC_CANDIDATES, names)
                if fabric_text is not None:
                    parsed = parse_fabric_json(fabric_text)
                    info.toml_source = fabric_source
                    _fill_from_info(info, parsed, fabric_source)
                else:
                    info.parse_error = "读不到 neoforge.mods.toml / mods.toml / fabric.mod.json"
            else:
                info.toml_source = source
                _fill_from_info(info, parse_mod_toml(text, source), source)

            scan = scan_jar(path, info.rel)
            info.jij_mod_ids = scan.jij_mod_ids
            info.problems = [f"{key}：{value}" for key, value in scan.problems.items()]
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, EOFError, RuntimeError) as exc:
        info.parse_error = f"jar 无法读取：{exc!r}"
    return info


def _fill_from_info(info: JarInfo, parsed: ModTomlInfo, source: str) -> None:
    """把解析结果填进 :class:`JarInfo`。"""
    info.mod_ids = list(parsed.mod_ids)
    primary = parsed.primary()
    if primary is not None:
        info.display_name = primary.display_name
        info.version = primary.version
        info.description = primary.description
        info.display_test = primary.display_test
        if primary.client_side_only is True:
            info.toml_side = "client"
            info.toml_side_detail = f"{source}: clientSideOnly=true"
        elif primary.client_side_only is False:
            # environment=server（Fabric）：纯服务端声明，同样是明确信号
            environment = primary.extra.get("environment", "")
            if environment == "server":
                info.toml_side = "server"
                info.toml_side_detail = f"{source}: environment=server"
