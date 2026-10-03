"""试迁移批次执行组件。

目录约定（root 内）：
- content/      业务文件根目录，组件不得在其中放置任何工作文件
- manifest.json 权限/元数据清单，以目标相对路径为键
- .pilot/       组件工作目录：ledger.db（结果账本）与 tmp/（覆盖前副本）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from pilot.ledger import Ledger

PASS_THRESHOLD = 49  # 至少 49 项成功才通过

_CONTENT_DIR = "content"
_MANIFEST_NAME = "manifest.json"
_WORK_DIR = ".pilot"
_LEDGER_NAME = "ledger.db"
_TMP_DIR = "tmp"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _missing_dirs(parent: Path, stop: Path) -> list[Path]:
    """返回 parent 向上至 stop 之间当前不存在的目录（自深向浅）。"""
    missing: list[Path] = []
    cursor = parent
    while cursor != stop and not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    return missing


def _restore_item(content_root: Path, backup_dir: Path, item: dict[str, Any]) -> None:
    """把单个条目恢复到批次开始前的状态。"""
    target = content_root / item["target"]
    backup = backup_dir / f"{item['index']}.bak"
    if item["existed_before"]:
        shutil.copyfile(backup, target)
    elif target.exists():
        target.unlink()
    for created in item["created_dirs"]:  # 已按自深向浅排列
        try:
            os.rmdir(created)
        except OSError:
            pass  # 目录非空（被其他条目使用）或已不存在


def run_pilot(root: str | Path, run_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """执行一个试迁移批次并返回结果；重复提交同一 run_id 返回原结果。

    每条 record 包含：
    - source: 源文件绝对路径（存在且不可变）
    - target: 目标相对路径（相对 root/content）
    - sha256: 预期 SHA-256（十六进制小写）
    - metadata / permissions: 已映射好的 JSON 值，原样写入清单
    """
    root_path = Path(root)
    content_root = root_path / _CONTENT_DIR
    manifest_path = root_path / _MANIFEST_NAME
    work_dir = root_path / _WORK_DIR
    ledger = Ledger(work_dir / _LEDGER_NAME)

    cached = ledger.get(run_id)
    if cached is not None:
        return cached

    backup_dir = work_dir / _TMP_DIR / run_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    content_root.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    try:
        for index, record in enumerate(records):
            target_rel = record["target"]
            target = content_root / target_rel
            item: dict[str, Any] = {
                "index": index,
                "target": target_rel,
                "record": record,
                "existed_before": target.exists(),
                "created_dirs": _missing_dirs(target.parent, content_root),
                "ok": False,
                "error": None,
            }
            try:
                if item["existed_before"]:
                    shutil.copyfile(target, backup_dir / f"{index}.bak")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(record["source"], target)
                actual = _sha256_of(target)
                if actual == record["sha256"].lower():
                    item["ok"] = True
                else:
                    item["error"] = f"sha256 mismatch: expected {record['sha256']}, got {actual}"
            except Exception as exc:  # 源读取/写入等错误均记为该项失败
                item["error"] = f"{type(exc).__name__}: {exc}"
            items.append(item)

        succeeded = [it for it in items if it["ok"]]
        failed = [it for it in items if not it["ok"]]
        passed = len(succeeded) >= PASS_THRESHOLD

        if passed:
            for item in reversed(failed):
                _restore_item(content_root, backup_dir, item)
            _update_manifest(manifest_path, succeeded)
            status = "passed"
        else:
            for item in reversed(items):
                _restore_item(content_root, backup_dir, item)
            status = "rolled_back"

        result = {
            "run_id": run_id,
            "status": status,
            "total": len(items),
            "succeeded": len(succeeded),
            "failed_targets": [it["target"] for it in failed],
            "errors": {it["target"]: it["error"] for it in failed},
        }
        ledger.put(run_id, result)
        return result
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)
        try:
            os.rmdir(backup_dir.parent)  # tmp/ 为空时一并移除
        except OSError:
            pass


def _update_manifest(manifest_path: Path, succeeded: list[dict[str, Any]]) -> None:
    """通过时仅更新成功项；其余条目保持语义不变。"""
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {}
    for item in succeeded:
        record = item["record"]
        manifest[item["target"]] = {
            "sha256": record["sha256"].lower(),
            "metadata": record["metadata"],
            "permissions": record["permissions"],
        }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, manifest_path)
