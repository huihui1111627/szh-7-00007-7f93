# 测序运行监控与处置核心（参考实现）

事件溯源（event-sourced）的测序运行控制内核：统一呈现多区域运行状态，预估人工干预
对后续批次与最终有效数据量的影响，对样本标识冲突 / 信号串扰 / 试剂不足做**局部隔离**，
并以三种恢复路径处置；进程重启后仅靠事件日志即可完整重建，哈希链防篡改。

## 运行

```bash
python3 demo.py            # 端到端示例（三类故障 + 三种恢复 + 重启审计）
python3 -m unittest discover -s tests -v
```

仅依赖 Python 3.9+ 标准库。

## 代码

- `sequencing/domain.py`：运行 / 区域 / 试剂 / 事件单的值对象与枚举。
- `sequencing/events.py`：只追加 JSONL 事件存储、SHA-256 哈希链、重放与篡改校验。
- `sequencing/engine.py`：推进、在线操作、`preview_action()` 影响推算、隔离与
  `recommend_recovery()` 三路径裁决、`snapshot()` 统一视图。
- `DESIGN.md`：设计说明（领域模型、隔离规则、有效数据量公式、审计机制）。
- `demo.py`、`tests/test_engine.py`：示例与 19 个单测。

## 快速上手

```python
from sequencing import EventStore, SequencingEngine, RunConfig, RegionSpec, ReagentSpec

cfg = RunConfig("RUN-1",
                regions=[RegionSpec("L1", "SAM-1", 20, 5_000_000, 150, 2.0, "KIT-A")],
                reagents=[ReagentSpec("KIT-A", 120.0)])
eng = SequencingEngine(EventStore("run.jsonl"), cfg)
eng.start_run(operator="zhang.lab")
print(eng.preview_action("pause", "L1", minutes=30))  # 执行前影响预估
eng.advance(5)                                         # 推进 5 个成像周期
print(eng.snapshot())                                  # 统一状态视图
```
