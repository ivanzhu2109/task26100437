"""试迁移组件自动化测试。

覆盖：
- 49 项成功（1 项校验失败）→ 通过：成功项保留、失败项恢复、清单仅更新成功项
- 48 项成功（2 项校验失败）→ 整批回滚：content 与清单逐字节回到批次前
- 混合场景：既有文件覆盖、新建嵌套目录、无关文件不受影响
- 重复提交同一 run_id：返回原结果、不重新执行
- 批次结束后无遗留临时内容或覆盖前副本
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot import run_pilot  # noqa: E402

BATCH_SIZE = 50


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_tree(root: Path) -> dict[str, bytes]:
    """返回 root 下所有目录与文件的快照：目录记为空 bytes，文件记为内容。"""
    snap: dict[str, bytes] = {}
    if not root.exists():
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            rel = (Path(dirpath) / d).relative_to(root).as_posix()
            snap[f"dir:{rel}"] = b""
        for f in filenames:
            p = Path(dirpath) / f
            snap[f"file:{p.relative_to(root).as_posix()}"] = p.read_bytes()
    return snap


class PilotTestBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.content = self.root / "content"
        self.sources = self.root / "sources"
        self.content.mkdir(parents=True)
        self.sources.mkdir()

        # 无关文件：批次前后必须逐字节不变
        (self.content / "unrelated").mkdir()
        (self.content / "unrelated" / "keep.bin").write_bytes(b"keep-me" * 64)

        # 既有文件：将被批次中的部分条目覆盖
        self.preexisting: dict[str, bytes] = {}
        for i in range(10):
            rel = f"existing/file-{i:02d}.dat"
            data = f"old-content-{i}".encode() * 32
            p = self.content / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            self.preexisting[rel] = data

        # 既有清单：包含无关条目与即将被覆盖的条目
        self.manifest_path = self.root / "manifest.json"
        self.base_manifest = {
            "unrelated/keep.bin": {
                "sha256": sha256_bytes(b"keep-me" * 64),
                "metadata": {"标签": "无关", "nested": {"a": [1, 2, 3]}},
                "permissions": {"mode": "0644", "owner": "root"},
            }
        }
        for rel, data in self.preexisting.items():
            self.base_manifest[rel] = {
                "sha256": sha256_bytes(data),
                "metadata": {"old": True},
                "permissions": {"mode": "0600"},
            }
        self.manifest_path.write_text(
            json.dumps(self.base_manifest, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )

    def make_records(self, fail_indexes: set[int]) -> tuple[list[dict], dict[str, bytes]]:
        """构造 50 条记录；fail_indexes 中的条目故意写错预期摘要。

        返回 (records, 期望写入成功的目标字节)。
        """
        records: list[dict] = []
        expected_bytes: dict[str, bytes] = {}
        for i in range(BATCH_SIZE):
            data = f"new-payload-{i}".encode() * 48
            src = self.sources / f"src-{i:02d}.bin"
            src.write_bytes(data)
            if i < 10:
                target = f"existing/file-{i:02d}.dat"  # 覆盖既有文件
            elif i < 30:
                target = f"nested/group-{i:02d}/deep/item-{i:02d}.dat"  # 新建嵌套目录
            else:
                target = f"flat/item-{i:02d}.dat"
            digest = sha256_bytes(data)
            if i in fail_indexes:
                digest = "0" * 64  # 故意不匹配
            else:
                expected_bytes[target] = data
            records.append(
                {
                    "source": str(src),
                    "target": target,
                    "sha256": digest,
                    "metadata": {"序号": i, "tags": ["pilot", f"g{i}"]},
                    "permissions": {"mode": "0640", "owner": "迁移者"},
                }
            )
        return records, expected_bytes

    def work_tmp_dir(self) -> Path:
        return self.root / ".pilot" / "tmp"


class TestPilotPass(PilotTestBase):
    """49 项成功、1 项失败 → 批次通过。"""

    def test_pass_with_49_successes(self) -> None:
        fail_index = 7  # 一个覆盖既有文件的条目失败
        records, expected_bytes = self.make_records({fail_index})
        failed_target = records[fail_index]["target"]

        result = run_pilot(self.root, "run-pass", records)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["succeeded"], 49)
        self.assertEqual(result["total"], 50)
        self.assertEqual(result["failed_targets"], [failed_target])

        # 成功项：目标字节与源一致
        for rel, data in expected_bytes.items():
            self.assertEqual((self.content / rel).read_bytes(), data, rel)

        # 失败项：恢复到批次前的既有内容
        self.assertEqual(
            (self.content / failed_target).read_bytes(),
            self.preexisting[failed_target],
        )

        # 无关文件不变
        self.assertEqual((self.content / "unrelated" / "keep.bin").read_bytes(), b"keep-me" * 64)

        # 清单：成功项已更新且输入 JSON 原样保留；失败项与无关条目保持原值
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        by_target = {r["target"]: r for r in records}
        for rel in expected_bytes:
            entry = manifest[rel]
            rec = by_target[rel]
            self.assertEqual(entry["sha256"], rec["sha256"])
            self.assertEqual(entry["metadata"], rec["metadata"])
            self.assertEqual(entry["permissions"], rec["permissions"])
        self.assertEqual(manifest[failed_target], self.base_manifest[failed_target])
        self.assertEqual(
            manifest["unrelated/keep.bin"], self.base_manifest["unrelated/keep.bin"]
        )
        new_paths = sum(1 for rel in expected_bytes if rel not in self.base_manifest)
        self.assertEqual(len(manifest), len(self.base_manifest) + new_paths)

        # 无遗留临时内容或覆盖前副本
        self.assertFalse(self.work_tmp_dir().exists())

        # 重复提交：篡改源文件后再提交，结果与文件系统均不变
        (self.sources / "src-00.bin").write_bytes(b"tampered")
        content_before = snapshot_tree(self.content)
        manifest_before = self.manifest_path.read_bytes()
        again = run_pilot(self.root, "run-pass", records)
        self.assertEqual(again, result)
        self.assertEqual(snapshot_tree(self.content), content_before)
        self.assertEqual(self.manifest_path.read_bytes(), manifest_before)


class TestPilotRollback(PilotTestBase):
    """48 项成功、2 项失败 → 整批回滚。"""

    def test_rollback_with_48_successes(self) -> None:
        records, _ = self.make_records({3, 41})  # 一个覆盖项 + 一个新建项失败
        content_before = snapshot_tree(self.content)
        manifest_before = self.manifest_path.read_bytes()

        result = run_pilot(self.root, "run-rollback", records)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["succeeded"], 48)
        self.assertEqual(sorted(result["failed_targets"]), sorted([records[3]["target"], records[41]["target"]]))

        # content 路径集合与文件字节完全恢复
        self.assertEqual(snapshot_tree(self.content), content_before)
        # 清单原始字节不变
        self.assertTrue(self.manifest_path.exists())
        self.assertEqual(self.manifest_path.read_bytes(), manifest_before)
        # 无遗留临时内容或覆盖前副本
        self.assertFalse(self.work_tmp_dir().exists())

        # 重复提交：返回原结果，不重新执行
        (self.sources / "src-05.bin").write_bytes(b"tampered")
        again = run_pilot(self.root, "run-rollback", records)
        self.assertEqual(again, result)
        self.assertEqual(snapshot_tree(self.content), content_before)
        self.assertEqual(self.manifest_path.read_bytes(), manifest_before)


class TestPilotFreshRoot(unittest.TestCase):
    """首次使用允许空 root：缺失清单视为空清单。"""

    def test_empty_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "fresh-root"
            sources = Path(d) / "sources"
            sources.mkdir()
            records = []
            for i in range(BATCH_SIZE):
                data = f"payload-{i}".encode()
                src = sources / f"s{i}.bin"
                src.write_bytes(data)
                records.append(
                    {
                        "source": str(src),
                        "target": f"dir/item-{i:02d}.bin",
                        "sha256": sha256_bytes(data),
                        "metadata": {"i": i},
                        "permissions": {"mode": "0644"},
                    }
                )
            result = run_pilot(root, "run-fresh", records)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["succeeded"], 50)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest), 50)
            for i in range(50):
                self.assertEqual(
                    (root / "content" / "dir" / f"item-{i:02d}.bin").read_bytes(),
                    f"payload-{i}".encode(),
                )
            self.assertFalse((root / ".pilot" / "tmp").exists())


if __name__ == "__main__":
    unittest.main()
