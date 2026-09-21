# 基因测序运行监控与处置系统 — 设计说明

## 1. 目标与范围

设备运行期间，系统需要对一次测序运行（Run）下多个并行区域（Region，如泳道/芯片分区）
提供统一视图，并支持实验人员的在线干预。核心约束：

1. **统一呈现**：样本在各处理阶段的位置、成像周期、信号质量、试剂消耗、读取进度一屏可查。
2. **影响推算**：暂停区域、重新校准成像、降低读取速度等操作，必须预估对后续批次与
   最终有效数据量（usable bases）的影响，而不是盲目执行。
3. **局部隔离**：样本标识冲突、信号串扰、试剂不足只隔离受影响范围，**绝不把整次运行
   判定为失败**；其他区域继续产出。
4. **三种恢复路径**：`CONTINUE`（可继续读取）、`RERUN`（必须重跑）、
   `PARTIAL_KEEP`（只能保留已有结果）。
5. **重启可核对**：所有状态由只追加（append-only）事件日志重建，事件哈希链保证防篡改，
   进程重启后重放日志即可恢复全部状态与处置记录。

## 2. 领域模型

- **Run（运行）**：状态 `RUNNING / PAUSED / COMPLETED / FAILED`。`FAILED` 只在操作员
  显式终止时出现，任何自动故障都不会导致整运行失败。
- **Region（区域）**：
  - 阶段 `PENDING → IMAGING → BASECALLING → DONE`（可经 `QUARANTINED / RERUN_REQUIRED / PARTIAL / HELD`）。
  - 当前成像周期 `cycle / total_cycles`，周期即“位置/进度”的统一坐标。
  - 信号质量 `signal_q`（0–1）、读取速率倍率 `speed`（1.0 为标称）。
  - 已读原始碱基数与可用比例（受质量与污染区间影响）。
- **Reagent（试剂）**：按 SKU 记录总量与已耗；每个区域每周期消耗固定体积。
- **Batch（后续批次）**：区域完成后进入下游批次的释放时间；操作用“批次延迟分钟数”
  量化对后续批次的影响。
- **Incident（事件单）**：
  - 类型 `SAMPLE_ID_CONFLICT / SIGNAL_CROSSTALK / REAGENT_SHORTAGE`。
  - 作用域为区域集合（冲突为涉及双方，串扰为相邻区域区间，缺剂为当前断供区域），
    其他区域不被波及。
  - 恢复裁决为上述三种路径之一，裁决理由随事件持久化。

## 3. 统一状态视图

`snapshot()` 聚合每个区域的：样本编号、阶段、周期位置、信号质量、速率、
原始/有效数据量、所属事件单；以及试剂余量、运行整体进度与预计完成时间（ETA）。

有效数据量（usable bases）：

```
region_raw      = cycles_done * cluster_count * read_len
usable_fraction = 有效区间比例 * 质量系数 q/(q+0.25)
region_usable   = region_raw * usable_fraction
run_usable      = Σ 未判为 RERUN 区域的 region_usable
```

质量系数为可复现的确定性函数，保证影响推算与实际记账使用同一公式。

## 4. 操作与影响推算

所有操作先经 `preview_action()` 给出 **执行前预估**，确认后才提交事件：

| 操作 | 影响模型 |
|---|---|
| 暂停/恢复区域 | 暂停分钟内该区域零产出；`批次延迟 = 暂停时长`；其他区域不受影响 |
| 重新校准成像 | 校准期间（默认 20 分钟）不产周期；完成后质量从 q 提升至 `q+(1-q)*0.4`，有效数据量回升 |
| 降低读取速度 | 倍率由 1.0 降到 0.6：单周期时长 ×(1/0.6)，ETA 延后；质量按 `q' = 1-(1-q)*0.6` 小幅改善 |

预估输出：`eta_delay_min`（对运行完成时间）、`batch_delay_min`（对后续批次释放）、
`usable_delta_bases`（最终有效数据量增减，乐观/保守两档）。

## 5. 故障隔离规则

- **样本标识冲突**：同一 `sample_id` 出现在两个区域。仅隔离这两个区域；
  操作员重新映射其一（`remap_sample`）后 → `CONTINUE`。
- **信号串扰**：受影响区域区间进入隔离；校准前的周期记为污染区间。
  - 污染占比 ≤ 20% → 可计算补救（重新校准后继续）→ `CONTINUE`
  - 20%–60% → 污染前数据保留、污染区切除 → `PARTIAL_KEEP`
  - > 60% 或已接近末尾且无法补救 → `RERUN`
- **试剂不足**：按 SKU 检查下一周期所需余量，仅把断供区域置 `HELD`（非整运行失败）；
  补剂并解除后 → `CONTINUE`；若周期已耗尽且未补剂 → `PARTIAL_KEEP`。

隔离只改受影响区域状态与新增 Incident；快照里始终可见“受影响范围 vs 正常范围”。

## 6. 三种恢复路径

`recommend_recovery(incident_id)` 是纯函数，依据事件单类型与当前数据给出裁决与理由：

- `CONTINUE`：根因可消除且数据连续（重映射、补剂、小范围串扰重校准）。
- `RERUN`：有效数据不可恢复（大范围污染），已有原始数据留档但不计入有效产出。
- `PARTIAL_KEEP`：不可继续但已产出的前缀数据有效，冻结保留。

`decide_recovery()` 记录裁决；`resolve_incident()` 据裁决驱动区域状态迁移
（继续读取 / 标记重跑 / 冻结已有结果），全部迁移落事件。

## 7. 事件溯源与重启审计

- 领域事件 15 种：`RunStarted / RegionActivated / CycleAdvanced / RegionFinished /
  RegionPaused / RegionResumed / CalibrationStarted / CalibrationCompleted /
  SpeedChanged / IncidentRaised / IncidentScopeAdded / SampleRemapped /
  ReagentReplenished / RecoveryDecided / IncidentResolved / RunCompleted`，
  只追加写入 JSONL，每条含 `seq / ts / type / data / operator / prev_hash / hash`。
  （运行级 `FAILED` 状态保留给操作员显式终止，自动故障永不触发。）
- 哈希为对前序哈希与事件内容的 SHA-256，形成链式结构：
  重放时重算哈希即可发现任何篡改或缺序。
- `EventStore.replay()` 从日志重建聚合；重启后打开同一日志文件即可继续，
  序列号严格延续。审计接口可导出“操作—事件单—裁决”全链路。

## 8. 代码结构

- `sequencing/domain.py`：枚举、配置与值对象。
- `sequencing/events.py`：只追加事件存储、哈希链与重放/校验。
- `sequencing/engine.py`：运行聚合（推进、操作、影响推算、隔离、恢复裁决）。
- `demo.py`：端到端示例（正常推进 → 三类故障 → 三种恢复路径 → 重启校验）。
- `tests/`：unittest 用例。
