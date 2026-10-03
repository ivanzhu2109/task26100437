import hashlib
import json
from pathlib import Path

import pytest

from pilot import PilotRecord, run_pilot

TOTAL = 50


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_sources(base: Path) -> list[Path]:
    src = base / "src"
    src.mkdir()
    paths = []
    for i in range(TOTAL):
        p = src / f"f{i}.bin"
        p.write_bytes(f"payload-{i}-".encode() * (i + 3))
        paths.append(p)
    return paths


def make_records(src_paths, targets, bad=()):
    records = []
    for i, (sp, tgt) in enumerate(zip(src_paths, targets)):
        digest = sha256(sp.read_bytes())
        if i in bad:
            digest = "00" * 32  # 故意制造校验失败
        records.append(
            PilotRecord(
                source=str(sp),
                target=tgt,
                sha256=digest,
                metadata={"idx": i, "tags": ["αβ", str(i)], "ratio": 0.5},
                permissions={"mode": 0o640, "acl": ["u:rw", "g:r"]},
            )
        )
    return records


def build_targets():
    targets = []
    for i in range(TOTAL):
        if i < 10:
            targets.append(f"existing/file{i}.dat")      # 覆盖既有文件
        elif i < 40:
            targets.append(f"new/deep/{i}/file{i}.bin")  # 新建嵌套目录
        else:
            targets.append(f"top/{i}.bin")               # 新建一级目录
    return targets


def snapshot_root(root: Path) -> dict:
    """content 内路径集合+字节，以及清单存在性与原始字节。"""
    state = {}
    content = root / "content"
    if content.exists():
        for p in sorted(content.rglob("*")):
            rel = p.relative_to(root).as_posix()
            state[rel] = p.read_bytes() if p.is_file() else None
    manifest = root / "manifest.json"
    state["manifest.json"] = manifest.read_bytes() if manifest.exists() else "<ABSENT>"
    return state


def populate_root(root: Path, with_manifest=True):
    content = root / "content"
    (content / "existing").mkdir(parents=True)
    for i in range(10):
        (content / "existing" / f"file{i}.dat").write_bytes(f"OLD-{i}".encode())
    (content / "unrelated").mkdir()
    (content / "unrelated" / "keep.txt").write_bytes(b"do-not-touch")
    if with_manifest:
        (root / "manifest.json").write_bytes(
            json.dumps(
                {
                    "existing/file7.dat": {"sha256": "old", "metadata": {"x": 1},
                                           "permissions": {"mode": 384}},
                    "unrelated/keep.txt": {"sha256": "keep", "metadata": {},
                                           "permissions": {}},
                },
                ensure_ascii=False,
            ).encode()
            + b"\n"
        )


def assert_no_leftovers(root: Path):
    work = root / ".pilot"
    assert not (work / "tmp").exists()
    leftovers = [p for p in work.rglob("*") if p.name != "ledger.db"]
    assert leftovers == []


# ---------------------------------------------------------------- 49 成功 → 通过

def test_pass_with_49_successes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    populate_root(root)
    src = make_sources(tmp_path)
    records = make_records(src, build_targets(), bad={7})
    before = snapshot_root(root)

    result = run_pilot(root, "run-pass", records)

    assert result.status == "passed"
    assert (result.total, result.succeeded, result.failed) == (TOTAL, 49, 1)
    assert not result.items[7].ok and "mismatch" in result.items[7].error
    assert all(it.ok for i, it in enumerate(result.items) if i != 7)

    content = root / "content"
    # 成功项已落到真实位置且字节正确
    for i, rec in enumerate(records):
        if i != 7:
            assert (content / rec.target).read_bytes() == src[i].read_bytes()
    # 失败项（覆盖既有文件）已恢复到批次前字节
    assert (content / "existing" / "file7.dat").read_bytes() == b"OLD-7"
    # 无关文件未受影响
    assert (content / "unrelated" / "keep.txt").read_bytes() == b"do-not-touch"

    # 清单：仅更新成功项，输入 JSON 值原样保留，其他条目语义不变
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["existing/file7.dat"] == {
        "sha256": "old", "metadata": {"x": 1}, "permissions": {"mode": 384}}
    assert manifest["unrelated/keep.txt"] == {
        "sha256": "keep", "metadata": {}, "permissions": {}}
    assert len(manifest) == 2 + 49
    for i, rec in enumerate(records):
        if i == 7:
            continue
        entry = manifest[rec.target]
        assert entry["sha256"] == rec.sha256
        assert entry["metadata"] == {"idx": i, "tags": ["αβ", str(i)], "ratio": 0.5}
        assert entry["permissions"] == {"mode": 0o640, "acl": ["u:rw", "g:r"]}

    assert_no_leftovers(root)

    # 重复提交：命中账本，不重新执行，结果与状态稳定
    replay = run_pilot(root, "run-pass", records)
    assert replay.replayed and replay.status == "passed"
    assert replay.succeeded == 49 and replay.failed == 1
    assert [(it.target, it.ok, it.error) for it in replay.items] == [
        (it.target, it.ok, it.error) for it in result.items
    ]
    after = snapshot_root(root)
    assert after != before  # 批次确实生效
    assert after == snapshot_root(root)


# ---------------------------------------------------------------- 48 成功 → 整批回滚

def test_rollback_with_48_successes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    populate_root(root)
    src = make_sources(tmp_path)
    # 一个覆盖项 + 一个新建嵌套项失败
    records = make_records(src, build_targets(), bad={7, 25})
    before = snapshot_root(root)

    result = run_pilot(root, "run-rollback", records)

    assert result.status == "rolled_back"
    assert (result.succeeded, result.failed) == (48, 2)

    # content 路径集合与字节、清单原始字节与批次前完全一致
    assert snapshot_root(root) == before
    assert_no_leftovers(root)

    # 重复提交：返回原结果，不重新执行
    replay = run_pilot(root, "run-rollback", records)
    assert replay.replayed and replay.status == "rolled_back"
    assert replay.succeeded == 48
    assert snapshot_root(root) == before


# ---------------------------------------------------------------- 缺失清单

def test_missing_manifest_treated_as_empty(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    populate_root(root, with_manifest=False)
    src = make_sources(tmp_path)
    records = make_records(src, build_targets(), bad=set())
    before = snapshot_root(root)
    assert before["manifest.json"] == "<ABSENT>"

    result = run_pilot(root, "run-nomanifest", records)
    assert result.status == "passed" and result.succeeded == TOTAL
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest) == TOTAL

    # 回滚场景下缺失清单不得被凭空创建
    root2 = tmp_path / "root2"
    root2.mkdir()
    populate_root(root2, with_manifest=False)
    records2 = make_records(src, build_targets(), bad={1, 2})
    before2 = snapshot_root(root2)
    result2 = run_pilot(root2, "run-nomanifest-rb", records2)
    assert result2.status == "rolled_back"
    assert snapshot_root(root2) == before2
    assert not (root2 / "manifest.json").exists()
