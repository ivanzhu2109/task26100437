# 本地文件迁移试迁移组件

对 `root/content` 执行 50 文件金丝雀批次迁移，失败时按规则恢复或整批回滚。
纯 Python 3.11+ 标准库实现，持久化仅用 SQLite 与本地文件，不启动任何服务。

## 布局约定

- `root/content/`：业务文件。组件绝不向其中写入工作文件。
- `root/manifest.json`：清单，以目标相对路径为键，保存 `sha256` / `metadata` / `permissions`。
- `root/.pilot/`：组件工作区（`ledger.db` 结果账本、临时目录）。批次结束后临时目录即被清除。

## 使用

```python
from pilot import PilotRecord, run_pilot

records = [
    PilotRecord(
        source="/abs/path/to/src0.bin",   # 源文件（存在且不可变）
        target="new/deep/0/file0.bin",    # 目标相对路径（相对 root/content）
        sha256="<预期SHA-256十六进制>",
        metadata={"idx": 0},              # 任意 JSON 值，原样写入清单
        permissions={"mode": 0o640},
    ),
    # ... 共 50 条
]

result = run_pilot("root", "run-001", records)
print(result.status, result.succeeded, result.failed)  # passed / rolled_back
```

## 批次语义

- 每项先写入真实目标位置，再回读目标校验 SHA-256；单项失败不中断批次。
- 50 项全部处理完后：≥49 成功则通过——保留成功项、失败项恢复到批次前状态、
  清单仅更新成功项；≤48 成功则整批回滚——`content` 路径集合与字节、
  清单存在性及原始字节与批次前完全一致。
- 同一 `run_id` 重复提交直接返回账本中的原结果（`result.replayed == True`），不重新执行。
- 批次结束后不留临时文件或覆盖前副本，仅保留 `root/.pilot/ledger.db` 账本。

## 测试

```bash
python3 -m pytest tests -q
```

覆盖：49 项成功（通过）、48 项成功（整批回滚）、缺失清单；混合既有文件覆盖、
新建嵌套目录与无关文件；逐字节核对文件与清单，并重复提交验证结果稳定、无遗留副本。
