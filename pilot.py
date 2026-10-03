"""本地文件迁移试迁移组件（金丝雀批次 + 覆盖回滚）。

公开入口：run_pilot(root, run_id, records)

布局约定：
- root/content/      业务文件（组件不往里写任何工作文件）
- root/manifest.json 权限/元数据清单，以目标相对路径为键
- root/.pilot/       组件工作区：ledger.db（结果账本）与临时目录
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTENT_DIR = "content"
MANIFEST_NAME = "manifest.json"
WORK_DIR = ".pilot"


@dataclass
class PilotRecord:
    """单条迁移记录。"""

    source: str            # 源文件路径（绝对路径，存在且不可变）
    target: str            # 目标相对路径（相对 root/content）
    sha256: str            # 预期 SHA-256（十六进制小写）
    metadata: Any          # 已映射好的元数据 JSON 值
    permissions: Any       # 已映射好的权限 JSON 值


@dataclass
class ItemResult:
    target: str
    ok: bool
    error: str | None = None


@dataclass
class PilotResult:
    run_id: str
    status: str            # "passed" | "rolled_back"
    total: int
    succeeded: int
    failed: int
    items: list[ItemResult] = field(default_factory=list)
    replayed: bool = False  # True 表示命中账本、未重新执行


# ---------------------------------------------------------------- 账本

def _open_ledger(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        " run_id TEXT PRIMARY KEY,"
        " result_json TEXT NOT NULL)"
    )
    return conn


def _result_to_json(result: PilotResult) -> str:
    return json.dumps(
        {
            "run_id": result.run_id,
            "status": result.status,
            "total": result.total,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "items": [
                {"target": it.target, "ok": it.ok, "error": it.error}
                for it in result.items
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _result_from_json(run_id: str, payload: str) -> PilotResult:
    data = json.loads(payload)
    return PilotResult(
        run_id=data["run_id"],
        status=data["status"],
        total=data["total"],
        succeeded=data["succeeded"],
        failed=data["failed"],
        items=[ItemResult(i["target"], i["ok"], i.get("error")) for i in data["items"]],
        replayed=True,
    )


# ---------------------------------------------------------------- 快照与恢复

class _Snapshot:
    """批次开始前对 content 与清单的快照，用于失败项恢复或整批回滚。"""

    def __init__(self, root: Path, records: list[PilotRecord], backup_dir: Path):
        self.content = root / CONTENT_DIR
        self.manifest_path = root / MANIFEST_NAME
        self.manifest_existed = self.manifest_path.exists()
        self.manifest_bytes = (
            self.manifest_path.read_bytes() if self.manifest_existed else None
        )
        self.dirs_before: set[Path] = set()
        if self.content.exists():
            self.dirs_before = {
                p for p in self.content.rglob("*") if p.is_dir()
            }
        # 仅对会被覆盖的既有目标备份原始字节
        self.backups: dict[int, Path] = {}
        self.existed: dict[int, bool] = {}
        backup_dir.mkdir(parents=True, exist_ok=True)
        for idx, rec in enumerate(records):
            dest = self.content / rec.target
            self.existed[idx] = dest.exists()
            if self.existed[idx]:
                backup = backup_dir / str(idx)
                shutil.copyfile(dest, backup)
                self.backups[idx] = backup

    def restore_item(self, idx: int, target: str) -> None:
        """把单个目标恢复到批次开始前的状态。"""
        dest = self.content / target
        backup = self.backups.get(idx)
        if backup is not None:
            shutil.copyfile(backup, dest)
        elif dest.exists():
            dest.unlink()

    def restore_manifest(self) -> None:
        if self.manifest_existed:
            self.manifest_path.write_bytes(self.manifest_bytes)  # type: ignore[arg-type]
        elif self.manifest_path.exists():
            self.manifest_path.unlink()

    def prune_new_dirs(self) -> None:
        """删除本批次新建、且当前已为空的目录（深层优先）。"""
        if not self.content.exists():
            return
        new_dirs = [
            p
            for p in self.content.rglob("*")
            if p.is_dir() and p not in self.dirs_before
        ]
        for d in sorted(new_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()  # 仅当为空时成功
            except OSError:
                pass


# ---------------------------------------------------------------- 主流程

def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pilot(root: str | os.PathLike[str], run_id: str,
              records: list[PilotRecord]) -> PilotResult:
    """执行一个金丝雀批次；同一 run_id 重复提交返回原结果。"""
    root = Path(root)
    content = root / CONTENT_DIR
    manifest_path = root / MANIFEST_NAME
    work = root / WORK_DIR
    tmp_dir = work / "tmp" / run_id
    work.mkdir(parents=True, exist_ok=True)

    conn = _open_ledger(work / "ledger.db")
    try:
        row = conn.execute(
            "SELECT result_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            return _result_from_json(run_id, row[0])

        snapshot = _Snapshot(root, records, tmp_dir / "backup")
        items: list[ItemResult] = []
        try:
            # 1) 逐项写入真实目标位置并回读校验
            for idx, rec in enumerate(records):
                try:
                    dest = content / rec.target
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(rec.source, dest)
                    actual = _sha256_of(dest)
                    if actual != rec.sha256.lower():
                        raise ValueError(
                            f"sha256 mismatch: expected {rec.sha256}, got {actual}"
                        )
                    items.append(ItemResult(rec.target, True))
                except Exception as exc:  # 单项失败不中断批次
                    items.append(ItemResult(rec.target, False, str(exc)))

            succeeded = sum(1 for it in items if it.ok)
            passed = succeeded >= len(records) - 1

            if passed:
                # 2a) 通过：仅恢复失败项，更新清单中成功项
                for idx, it in enumerate(items):
                    if not it.ok:
                        snapshot.restore_item(idx, it.target)
                snapshot.prune_new_dirs()
                manifest: dict[str, Any] = {}
                if snapshot.manifest_existed:
                    manifest = json.loads(snapshot.manifest_bytes)  # type: ignore[arg-type]
                for rec, it in zip(records, items):
                    if it.ok:
                        manifest[rec.target] = {
                            "sha256": rec.sha256,
                            "metadata": rec.metadata,
                            "permissions": rec.permissions,
                        }
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                status = "passed"
            else:
                # 2b) 未通过：整批回滚到批次开始前
                for idx, it in enumerate(items):
                    snapshot.restore_item(idx, it.target)
                snapshot.restore_manifest()
                snapshot.prune_new_dirs()
                status = "rolled_back"

            result = PilotResult(
                run_id=run_id,
                status=status,
                total=len(records),
                succeeded=succeeded,
                failed=len(records) - succeeded,
                items=items,
            )
            conn.execute(
                "INSERT INTO runs (run_id, result_json) VALUES (?, ?)",
                (run_id, _result_to_json(result)),
            )
            conn.commit()
            return result
        finally:
            # 3) 批次结束后不留临时内容或覆盖前副本
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                (work / "tmp").rmdir()
            except OSError:
                pass
    finally:
        conn.close()
