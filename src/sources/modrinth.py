"""Modrinth 数据源：``sha1 -> version -> project -> client_side/server_side``。

实测命中率 **121/150 = 80.7%**（同一套 817 MB 整合包），有官方批量 API：

* ``POST /version_files``  ``{"hashes": [...], "algorithm": "sha1"}`` —— 一次 100 个
* ``GET  /projects?ids=[...]``  —— 返回 ``client_side`` / ``server_side``

**缺点**：字段由 mod 作者自己填，经常不准 —— 这正是本项目要改进的环节，
所以它只是**两个主源之一**，不是唯一依据。

``client_side`` / ``server_side`` 取值：``required`` / ``optional`` / ``unsupported``
/ ``unknown``。判定规则与 AutoSync 的 ``classify`` 保持一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..cache import CacheManager
from ..net import HttpClient, NetworkError
from ..verdict import (
    CONF_HIGH,
    CONF_MEDIUM,
    SIDE_BOTH,
    SIDE_CLIENT,
    SIDE_SERVER,
    SIDE_UNKNOWN,
    SideVerdict,
)

__all__ = [
    "SIDE_REQUIRED",
    "SIDE_OPTIONAL",
    "SIDE_UNSUPPORTED",
    "SIDE_UNKNOWN_VALUE",
    "is_unsupported",
    "side_from_project",
    "ModrinthProject",
    "ModrinthSource",
]

SIDE_REQUIRED = "required"
SIDE_OPTIONAL = "optional"
SIDE_UNSUPPORTED = "unsupported"
SIDE_UNKNOWN_VALUE = "unknown"


def is_unsupported(value: Any) -> bool:
    """该侧是否「不支持」（``unsupported``）。"""
    return str(value or "").strip().lower() == SIDE_UNSUPPORTED


@dataclass
class ModrinthProject:
    """Modrinth 项目摘要（只保留判定需要的字段）。"""

    project_id: str = ""
    slug: str = ""
    title: str = ""
    client_side: str = SIDE_UNKNOWN_VALUE
    server_side: str = SIDE_UNKNOWN_VALUE
    project_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "slug": self.slug,
            "title": self.title,
            "client_side": self.client_side,
            "server_side": self.server_side,
            "project_type": self.project_type,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModrinthProject":
        return cls(
            project_id=str(data.get("project_id") or ""),
            slug=str(data.get("slug") or ""),
            title=str(data.get("title") or ""),
            client_side=str(data.get("client_side") or SIDE_UNKNOWN_VALUE),
            server_side=str(data.get("server_side") or SIDE_UNKNOWN_VALUE),
            project_type=str(data.get("project_type") or ""),
        )


def side_from_project(project: ModrinthProject) -> SideVerdict:
    """把 Modrinth 的 side 字段翻成 :class:`SideVerdict`。

    **口径（项目所有者确认）：只有 ``unsupported`` 才算不需要；
    ``required`` 和 ``optional`` 一律视为需要。**

    ``optional`` 的语义是「可以装」，不是「不必装」。多判一端只是多发一个 mod
    （浪费带宽），少判一端会让那一端缺 mod 直接崩游戏 —— 代价不对等，所以取保守侧。

    规则（与 AutoSync ``classify`` 对齐）：

    * 两侧都 ``unknown`` -> 没用的信息，返回 unknown（**不采信**，否则会把作者
      没填的 mod 全判成双端，掩盖真正的不确定）；
    * ``server_side == unsupported``（且客户端侧有 ``required``/``optional``）->
      **纯客户端**；
    * ``client_side == unsupported``（且服务端侧有 ``required``/``optional``）->
      **纯服务端**；
    * 其余（两端都是 ``required``/``optional`` 的任意组合，含
      ``client=required + server=optional``）-> **双端**。
    """
    client_value = str(project.client_side or "").strip().lower()
    server_value = str(project.server_side or "").strip().lower()
    raw = {
        "project_id": project.project_id,
        "client_side": client_value,
        "server_side": server_value,
        "title": project.title or project.slug,
    }
    detail = f"modrinth client={client_value or '?'}, server={server_value or '?'}"

    if client_value == SIDE_UNKNOWN_VALUE and server_value == SIDE_UNKNOWN_VALUE:
        return SideVerdict.unknown("modrinth", detail + "（作者未填，不采信）", raw)

    if is_unsupported(client_value) and is_unsupported(server_value):
        return SideVerdict(
            side=SIDE_BOTH,
            confidence="low",
            source="modrinth",
            detail=detail + "（两端都标 unsupported，字段自相矛盾，保守判双端）",
            raw=raw,
        )

    if is_unsupported(server_value):
        return SideVerdict(SIDE_CLIENT, CONF_HIGH, "modrinth", detail + " -> 服务端不支持", raw)
    if is_unsupported(client_value):
        return SideVerdict(SIDE_SERVER, CONF_HIGH, "modrinth", detail + " -> 客户端不支持", raw)

    # 两端都被标成 required/optional（含 required+optional）—— 都算「要装」
    confidence = CONF_HIGH if (client_value == SIDE_REQUIRED and server_value == SIDE_REQUIRED) else CONF_MEDIUM
    return SideVerdict(SIDE_BOTH, confidence, "modrinth", detail + " -> 两端都可用", raw)


@dataclass
class ModrinthSource:
    """Modrinth 查询（带缓存）。"""

    client: HttpClient
    cache: CacheManager
    api_base: str = "https://api.modrinth.com/v2"
    batch_size: int = 100
    enabled: bool = True

    #: ``rel -> SideVerdict``（本次扫描已算过的）
    _computed: Dict[str, SideVerdict] = field(default_factory=dict, repr=False)
    #: 本次扫描遇到的可读错误（用于报告与「断网降级」提示）
    errors: List[str] = field(default_factory=list, repr=False)
    #: 每个 jar 用到的 sha1 -> 是否命中
    _hit_sha1: Dict[str, str] = field(default_factory=dict, repr=False)
    _project_cache: Dict[str, ModrinthProject] = field(default_factory=dict, repr=False)
    _sha1_project: Dict[str, str] = field(default_factory=dict, repr=False)
    _version_names: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    #: 本次扫描真正发出的 HTTP 请求数（用于验收「第二次扫描不重复请求」）
    requests_made: int = field(default=0, repr=False)

    # ------------------------------------------------------------ 缓存键
    @staticmethod
    def _sha1_key(sha1: str) -> str:
        return f"sha1:{sha1.lower()}"

    @staticmethod
    def _project_key(project_id: str) -> str:
        return f"project:{project_id}"

    # ------------------------------------------------------------ 主流程
    def lookup(
        self,
        items: Sequence[Tuple[str, str]],
        progress: Optional[Any] = None,
    ) -> Dict[str, SideVerdict]:
        """批量查询。

        :param items: ``[(rel, sha1), ...]``
        :param progress: 可选回调 ``(done, total, message)``
        :return: ``rel -> SideVerdict``（查询失败的条目不会出现在结果里）
        """
        self._computed = {}
        if not self.enabled:
            return {}
        pairs = [(str(rel), str(sha1).strip().lower()) for rel, sha1 in items if str(sha1 or "").strip()]
        if not pairs:
            return {}

        # 1) 先看缓存里有没有 sha1 的版本记录
        need_sha1: List[str] = []
        for _rel, sha1 in pairs:
            if sha1 in self._sha1_project:
                continue
            cached = self.cache.get_modrinth(self._sha1_key(sha1))
            if cached is not None:
                project_id = str(cached.get("project_id") or "")
                self._sha1_project[sha1] = project_id
                self._version_names[sha1] = {
                    "version_number": cached.get("version_number", ""),
                    "name": cached.get("name", ""),
                }
                continue
            if sha1 not in need_sha1:
                need_sha1.append(sha1)

        # 2) 批量查 sha1 -> project
        if need_sha1:
            self._fetch_versions(need_sha1, progress)

        # 3) 找出还缺 side 字段的 project
        need_projects: List[str] = []
        for _rel, sha1 in pairs:
            project_id = self._sha1_project.get(sha1)
            if not project_id:
                continue
            if project_id in self._project_cache:
                continue
            cached = self.cache.get_modrinth(self._project_key(project_id))
            if cached is not None:
                self._project_cache[project_id] = ModrinthProject.from_dict(cached)
                continue
            if project_id not in need_projects:
                need_projects.append(project_id)

        # 4) 批量查 project -> sides
        if need_projects:
            self._fetch_projects(need_projects, progress)

        # 5) 组装结果
        result: Dict[str, SideVerdict] = {}
        for rel, sha1 in pairs:
            project_id = self._sha1_project.get(sha1)
            if not project_id:
                continue
            project = self._project_cache.get(project_id)
            if project is None:
                continue
            verdict = side_from_project(project)
            version = self._version_names.get(sha1) or {}
            if version.get("version_number"):
                verdict.raw["version_number"] = version.get("version_number")
            if version.get("name"):
                verdict.raw["version_name"] = version.get("name")
            self._computed[rel] = verdict
            result[rel] = verdict
        return result

    # ------------------------------------------------------------ 网络
    def _fetch_versions(self, sha1s: List[str], progress: Optional[Any]) -> None:
        total = len(sha1s)
        for start in range(0, total, max(1, int(self.batch_size))):
            chunk = sha1s[start : start + max(1, int(self.batch_size))]
            try:
                self.requests_made += 1
                payload = self.client.post_json(
                    f"{self.api_base}/version_files", {"hashes": chunk, "algorithm": "sha1"}
                )
            except NetworkError as exc:
                self.errors.append(f"Modrinth 版本查询失败（{len(chunk)} 个 hash）：{exc}")
                if progress is not None:
                    progress(min(start + len(chunk), total), total, "Modrinth 版本查询失败，已降级")
                continue
            if isinstance(payload, dict):
                for sha1, version in payload.items():
                    key = str(sha1).strip().lower()
                    if not isinstance(version, dict):
                        continue
                    project_id = str(version.get("project_id") or "")
                    self._sha1_project[key] = project_id
                    self._version_names[key] = {
                        "version_number": version.get("version_number", ""),
                        "name": version.get("name", ""),
                    }
                    self.cache.put_modrinth(
                        self._sha1_key(key),
                        {
                            "project_id": project_id,
                            "version_number": version.get("version_number", ""),
                            "name": version.get("name", ""),
                        },
                    )
            # 没返回的 hash 也记一条空记录，避免每次都重查（但 TTL 更短由 cache 统一管）
            for sha1 in chunk:
                if sha1 not in self._sha1_project:
                    self._sha1_project[sha1] = ""
                    self.cache.put_modrinth(self._sha1_key(sha1), {"project_id": ""})
            if progress is not None:
                progress(min(start + len(chunk), total), total, "Modrinth 版本查询")

    def _fetch_projects(self, project_ids: List[str], progress: Optional[Any]) -> None:
        total = len(project_ids)
        batch = max(1, int(self.batch_size))
        for start in range(0, total, batch):
            chunk = project_ids[start : start + batch]
            ids_param = json.dumps(chunk, separators=(",", ":"))
            try:
                self.requests_made += 1
                payload = self.client.get_json(f"{self.api_base}/projects", params={"ids": ids_param})
            except NetworkError as exc:
                self.errors.append(f"Modrinth 项目查询失败（{len(chunk)} 个 id）：{exc}")
                if progress is not None:
                    progress(min(start + len(chunk), total), total, "Modrinth 项目查询失败，已降级")
                continue
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    project = ModrinthProject(
                        project_id=str(item.get("id") or ""),
                        slug=str(item.get("slug") or ""),
                        title=str(item.get("title") or ""),
                        client_side=str(item.get("client_side") or SIDE_UNKNOWN_VALUE),
                        server_side=str(item.get("server_side") or SIDE_UNKNOWN_VALUE),
                        project_type=str(item.get("project_type") or ""),
                    )
                    if project.project_id:
                        self._project_cache[project.project_id] = project
                        self.cache.put_modrinth(self._project_key(project.project_id), project.to_dict())
            if progress is not None:
                progress(min(start + len(chunk), total), total, "Modrinth 项目查询")

    # ------------------------------------------------------------ 统计
    @property
    def hit_count(self) -> int:
        """本次扫描中拿到有效 side 判定的 jar 数。"""
        return sum(1 for verdict in self._computed.values() if verdict.known)

    @property
    def resolved_count(self) -> int:
        """本次扫描中在 Modrinth 上找到项目的 jar 数（不论字段是否可用）。"""
        return len(self._computed)
