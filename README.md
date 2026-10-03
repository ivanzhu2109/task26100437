# 本地文件迁移试迁移组件

以 50 项为一个金丝雀批次执行试迁移：先写入真实目标位置并校验 SHA-256，
按成功数决定通过（≥49）或整批回滚（≤48），结果持久化在 SQLite 账本中，
重复提交同一 `run_id` 直接返回原结果。

## 目录约定

```
root/
├── content/        # 业务文件根目录（组件不在这里放任何工作文件）
├── manifest.json   # 清单：以目标相对路径为键，保存摘要/元数据/权限
└── .pilot/         # 组件工作目录（自动创建）
    ├── ledger.db   # SQLite 结果账本（批次完成后保留）
    └── tmp/        # 覆盖前副本，批次结束时清理，不留遗留
```

首次使用允许空 root；缺失 `manifest.json` 视为空清单。

## 接口

```python
from pilot import run_pilot

result = run_pilot(root, run_id, records)
```

- `root`：迁移根目录（字符串或 `Path`）。
- `run_id`：批次唯一标识；同一 `run_id` 只会提交同一份 `records`。
- `records`：50 条记录，每条包含：
  - `source`：源文件绝对路径（存在且不可变）
  - `target`：目标相对路径（相对 `root/content`）
  - `sha256`：预期 SHA-256（十六进制）
  - `metadata` / `permissions`：已映射好的 JSON 值，原样写入清单

返回字典：`status`（`passed` / `rolled_back`）、`succeeded`、`failed_targets`、
`errors` 等。重复提交返回与首次完全一致的结果，且不重新执行批次。

## 执行规则

1. 逐项把源文件写入真实目标位置，再读取目标校验 SHA-256；不符记为该项失败，
   继续处理其余项。覆盖既有文件前先把原字节备份到 `.pilot/tmp/<run_id>/`。
2. 全部处理完毕后：成功 ≥49 项 → 通过，保留成功项、失败项恢复到批次前状态，
   清单仅更新成功项（其余条目语义不变）；成功 ≤48 项 → 整批回滚，
   `content` 的路径集合、文件字节与清单原始字节完全恢复到批次前。
3. 批次结束后清理该批次的全部临时内容与覆盖前副本，仅保留结果账本。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：49 项成功（通过）、48 项成功（整批回滚）、空 root 首次使用；
混合既有文件覆盖、新建嵌套目录与无关文件；逐字节核对文件与清单；
并重复提交验证结果稳定、无遗留内容副本。

## 环境要求

Python ≥ 3.11，仅依赖标准库（`sqlite3`、`hashlib`、`pathlib` 等），
无需 Docker、数据库服务或任何监听端口的进程。
