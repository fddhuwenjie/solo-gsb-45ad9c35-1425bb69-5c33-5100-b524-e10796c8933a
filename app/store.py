"""活动版本的持久快照存储。

每个活动版本是一个**不可变快照**：路由原始输入、审计结果与 OpenAPI
paths 在发布时一次性计算并整体落盘，读取时从同一份快照返回三者，杜绝
把旧路由与新导出片段拼接。

持久化采用"写临时文件 + fsync + 原子 rename"，容器重启后从同一文件恢复；
并发发布以期望版本做乐观并发控制（CAS），相同期望版本只有一个成功。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

SNAPSHOT_FORMAT_VERSION = 1
_SNAPSHOT_FILE = "active-routes.json"


def initial_snapshot() -> dict[str, Any]:
    """版本 0：路由为空，审计与 paths 均为该空集的同一快照产物。"""
    from .publishing import validate_candidate

    check = validate_candidate([])
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "version": 0,
        "routes": [],
        "audit": check.audit.model_dump(),
        "paths": check.export.paths,
    }


class SnapshotStore:
    """线程安全、进程重启后可恢复的活动快照存储。"""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _SNAPSHOT_FILE
        self._lock = threading.RLock()
        self._snapshot = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return initial_snapshot()
        with self._path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key in ("version", "routes", "audit", "paths"):
            if key not in data:
                raise ValueError(f"snapshot file {self._path} is missing key {key!r}")
        return data

    def _persist(self, snapshot: dict[str, Any]) -> None:
        """临时文件写入并 fsync，再原子替换正式文件；目录也 fsync。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f".{_SNAPSHOT_FILE}.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self._path)
        try:
            dir_fd = os.open(self._data_dir, os.O_RDONLY)
        except (OSError, AttributeError):
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def current(self) -> dict[str, Any]:
        """返回当前活动快照（不可变语义：调用方不得修改）。"""
        with self._lock:
            return self._snapshot

    def current_version(self) -> int:
        with self._lock:
            return int(self._snapshot["version"])

    def publish(
        self,
        expected_version: int,
        routes_payload: list[dict[str, Any]],
        audit: dict[str, Any],
        paths: dict[str, Any],
    ) -> tuple[bool, int, dict[str, Any] | None]:
        """CAS 发布。

        返回 ``(是否发布成功, 当前版本, 成功时的新快照)``。两个使用相同
        ``expected_version`` 的并发发布只有一个能把新版本换入活动快照。
        """
        with self._lock:
            current_version = int(self._snapshot["version"])
            if expected_version != current_version:
                return False, current_version, None

            snapshot = {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "version": current_version + 1,
                "routes": routes_payload,
                "audit": audit,
                "paths": paths,
            }
            # 先落盘并 fsync，再换内存引用：崩溃时磁盘上至少有一个完整版本。
            self._persist(snapshot)
            self._snapshot = snapshot
            return True, snapshot["version"], snapshot
