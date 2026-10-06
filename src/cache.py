"""本地 JSON 缓存。

分五块：

============  ==================================================
``jars``      jar 元数据（含 sha1/sha256），键 = 相对路径 + 大小 + mtime
``mcmod_env`` MC 百科「运行环境」原文，键 = classId
``mcmod_cid`` 搜索名 -> classId，避免重复搜索（搜索是最大耗时项）
``modrinth``  sha1 -> version 与 project_id -> side 字段
``overrides`` 人工复核结论，键 = ``sha1:<...>`` 或 ``mod:<modId>``
============  ==================================================

两条硬约束：

1. **人工复核永远优先**，且不设过期时间 —— 那是人一次性确认过的结论；
2. mcmod 抓取内容为 BY-NC-SA 3.0，**本地缓存可以，但不要打包进分发的产物**。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["CacheManager", "jar_cache_key", "parse_iso", "now_iso"]

#: 缓存文件版本，结构不兼容时直接丢弃重建（缓存是可再生的，不必迁移）
CACHE_VERSION = 3
CACHE_FILENAME = "side-cache.json"


def now_iso() -> str:
    """本地时区的 ISO 时间戳（带偏移），报告里统一用它。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def parse_iso(text: str) -> float:
    """把 :func:`now_iso` 写出的时间戳还原成 epoch 秒；失败返回 0。"""
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except ValueError:
            continue
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def jar_cache_key(rel: str, size: int, mtime_ns: int) -> str:
    """jar 缓存键：路径 + 大小 + mtime，任一变化即视为「换过文件」。"""
    return f"{rel}|{int(size)}|{int(mtime_ns)}"


@dataclass
class CacheManager:
    """线程安全的 JSON 缓存。所有写入都走 :meth:`save` 的原子替换。"""

    path: Path
    #: 缓存有效期（小时）；<=0 表示永不过期
    ttl_hours: float = 720.0
    jars: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    mcmod_env: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    mcmod_cid: Dict[str, Any] = field(default_factory=dict)
    modrinth: Dict[str, Any] = field(default_factory=dict)
    overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _dirty: bool = field(default=False, repr=False)

    # ------------------------------------------------------------ 生命周期
    @classmethod
    def load(cls, path: Path, ttl_hours: float = 720.0) -> "CacheManager":
        """读缓存；文件缺失 / 版本不符 / 损坏时返回空缓存（不抛异常）。"""
        manager = cls(path=Path(path), ttl_hours=ttl_hours)
        try:
            with open(manager.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return manager
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return manager
        for name in ("jars", "mcmod_env", "mcmod_cid", "modrinth", "overrides"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(manager, name, value)
        return manager

    def save(self, force: bool = False) -> None:
        """原子写回缓存文件。没有改动且 ``force=False`` 时直接跳过。"""
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": CACHE_VERSION,
                "saved_at": now_iso(),
                "jars": self.jars,
                "mcmod_env": self.mcmod_env,
                "mcmod_cid": self.mcmod_cid,
                "modrinth": self.modrinth,
                "overrides": self.overrides,
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._dirty = False
            except OSError:
                # 缓存写失败不该影响主流程（例如目录只读）
                pass

    # ------------------------------------------------------------ 过期判断
    def expired(self, stamp: str, ttl_hours: Optional[float] = None) -> bool:
        """``stamp`` 是否已过期。``ttl_hours <= 0`` 时永不过期。"""
        hours = self.ttl_hours if ttl_hours is None else ttl_hours
        if hours is None or hours <= 0:
            return False
        fetched = parse_iso(stamp)
        if fetched <= 0:
            return True
        return (time.time() - fetched) > hours * 3600

    # ------------------------------------------------------------ jar 元数据
    def get_jar(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self.jars.get(key)
            return dict(item) if isinstance(item, dict) else None

    def put_jar(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self.jars[key] = value
            self._dirty = True
            self._prune_jars()

    def _prune_jars(self, limit: int = 5000) -> None:
        """jar 缓存过多时丢掉最旧的一批（按时间戳），防止文件无限膨胀。"""
        if len(self.jars) <= limit:
            return
        ordered = sorted(
            self.jars.items(),
            key=lambda item: float(item[1].get("cached_ts") or 0.0),
            reverse=True,
        )
        self.jars = dict(ordered[:limit])

    # ------------------------------------------------------------ mcmod
    def get_mcmod_class_id(self, name: str) -> Optional[int]:
        """已确认「搜不到」的名字缓存为 ``0``，避免每次都重搜。"""
        with self._lock:
            item = self.mcmod_cid.get(str(name or "").strip().lower())
        if isinstance(item, dict):
            if self.expired(str(item.get("at") or "")):
                return None
            value = item.get("cid")
        else:
            value = item
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get_mcmod_title(self, name: str) -> str:
        """缓存里记下的条目标题（用于「搜到的到底是不是这个 mod」的校验）。"""
        with self._lock:
            item = self.mcmod_cid.get(str(name or "").strip().lower())
        if isinstance(item, dict):
            return str(item.get("title") or "")
        return ""

    def put_mcmod_class_id(self, name: str, class_id: Optional[int], title: str = "") -> None:
        with self._lock:
            self.mcmod_cid[str(name or "").strip().lower()] = {
                "cid": int(class_id) if class_id else 0,
                "title": str(title or ""),
                "at": now_iso(),
            }
            self._dirty = True

    def get_mcmod_env(self, class_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self.mcmod_env.get(str(class_id))
            if not isinstance(item, dict):
                return None
            if self.expired(str(item.get("at") or "")):
                return None
            return dict(item)

    def put_mcmod_env(self, class_id: int, payload: Dict[str, Any]) -> None:
        with self._lock:
            record = dict(payload)
            record["at"] = now_iso()
            self.mcmod_env[str(class_id)] = record
            self._dirty = True

    # ------------------------------------------------------------ Modrinth
    def get_modrinth(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self.modrinth.get(str(key))
            if not isinstance(item, dict):
                return None
            if self.expired(str(item.get("at") or "")):
                return None
            return dict(item)

    def put_modrinth(self, key: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            record = dict(payload)
            record["at"] = now_iso()
            self.modrinth[str(key)] = record
            self._dirty = True

    # ------------------------------------------------------------ 人工复核
    def get_override(self, sha1: str = "", mod_id: str = "") -> Optional[Dict[str, Any]]:
        """人工复核结论：先按 sha1（精确到文件），再按 modId（跨版本）。"""
        with self._lock:
            for key in override_keys(sha1, mod_id):
                item = self.overrides.get(key)
                if isinstance(item, dict):
                    return dict(item)
        return None

    def put_override(self, side: str, sha1: str = "", mod_id: str = "", note: str = "") -> None:
        """记录人工复核。``sha1`` 优先；两者都没有时抛 :class:`ValueError`。"""
        keys = override_keys(sha1, mod_id)
        if not keys:
            raise ValueError("人工复核至少需要 sha1 或 mod_id 之一")
        with self._lock:
            record = {"side": side, "note": note, "at": now_iso()}
            for key in keys:
                self.overrides[key] = dict(record)
            self._dirty = True

    def drop_override(self, sha1: str = "", mod_id: str = "") -> None:
        with self._lock:
            for key in override_keys(sha1, mod_id):
                if self.overrides.pop(key, None) is not None:
                    self._dirty = True

    def override_count(self) -> int:
        with self._lock:
            return len(self.overrides)

    # ------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "jars": len(self.jars),
                "mcmod_env": len(self.mcmod_env),
                "mcmod_cid": len(self.mcmod_cid),
                "modrinth": len(self.modrinth),
                "overrides": len(self.overrides),
            }


def override_keys(sha1: str, mod_id: str) -> List[str]:
    """人工复核的键（sha1 精确优先，modId 跨版本兜底）。"""
    keys: List[str] = []
    if str(sha1 or "").strip():
        keys.append(f"sha1:{str(sha1).strip().lower()}")
    if str(mod_id or "").strip():
        keys.append(f"mod:{str(mod_id).strip().lower()}")
    return keys
