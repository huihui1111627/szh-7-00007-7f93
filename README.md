# 测序运行监控与处置系统（seqrun）

面向基因测序设备运行期间的统一监控、人工操作影响推算、故障隔离与恢复、
以及全过程重启可核对的一套事件溯源（event-sourced）领域系统。仅使用
Python 3.9+ 标准库，无第三方依赖。

## 需求对应

| 需求 | 实现 |
| --- | --- |
| 统一呈现位置、阶段、成像周期、信号质量、试剂消耗、读取进度 | `seqrun/dashboard.py`：文本看板 + 单文件 HTML；数据为 `Run/Batch/Region/Sample` 四级模型 |
| 暂停区域 / 重校准 / 降速，并推算对后续批次与有效数据量影响 | `pause_region`、`recalibrate`、`set_speed` 命令；`seqrun/impact.py` 的 what-if 投影（延误连锁、Q30 与 Gb 增减） |
| 标识冲突 / 信号串扰 / 试剂不足按范围隔离，不判整次运行失败 | 故障作用域为 sample/region，仅受影响样本进入 `ISOLATED`；其余样本继续成像 |
| 三种恢复路径 | `CONTINUE_READ`（可继续读取）、`RERUN`（必须重跑）、`KEEP_EXISTING`（仅保留已有结果），标识冲突另用 `REMAP`；规则见 `seqrun/recovery.py` |
| 重启后仍可核对 | 所有状态变更只通过事件写入 JSONL 日志，事件以 SHA-256 哈希链串联；重启先校验链再重放（`seqrun/events.py`、`store.load/audit`） |

## 模块结构

```
seqrun/
  model.py      领域模型：Run/Batch/Region/Sample、阶段、故障、恢复动作
  events.py     事件、哈希链日志（追加写、校验）
  engine.py     命令处理、成像节拍推进、故障自动检测、事件 reducer（重放）
  recovery.py   三种恢复路径的判定规则与建议
  impact.py     what-if 影响推算：批次延误、Q30、最终有效 Gb
  dashboard.py  统一文本看板与 HTML 导出
  store.py      运行实例的创建/加载/重启核对
  cli.py        命令行与端到端 demo
tests/
  test_seqrun.py  12 个行为测试
```

## 快速开始

```bash
# 端到端演示：看板、三类故障、三种恢复、what-if、重启核对
python3 -m seqrun.cli demo --root .runs

# 文本看板 / JSON / HTML
python3 -m seqrun.cli status RUN-DEMO --root .runs
python3 -m seqrun.cli json   RUN-DEMO --root .runs
python3 -m seqrun.cli html   RUN-DEMO --root .runs

# 重启核对（哈希链 + 事件重放）
python3 -m seqrun.cli verify RUN-DEMO --root .runs

# what-if：暂停 RG-A 20 分钟 / RG-B 降到 0.5x / 重校准到 0.0
python3 -m seqrun.cli whatif RUN-DEMO pause RG-A 20 --root .runs
python3 -m seqrun.cli whatif RUN-DEMO speed RG-B 0.5 --root .runs
python3 -m seqrun.cli whatif RUN-DEMO recalibrate RG-A 0.0 --root .runs
```

实际操作命令会追加事件并立即生效：

```bash
python3 -m seqrun.cli pause     RUN-DEMO RG-A --minutes 15 --root .runs
python3 -m seqrun.cli resume    RUN-DEMO RG-A --root .runs
python3 -m seqrun.cli recalibrate RUN-DEMO RG-A 0.1 --root .runs
python3 -m seqrun.cli speed     RUN-DEMO RG-B 0.5 --root .runs
python3 -m seqrun.cli tick      RUN-DEMO 10 --root .runs   # 推进 10 分钟
python3 -m seqrun.cli resolve   RUN-DEMO xt-1 continue_read --root .runs
python3 -m seqrun.cli resolve   RUN-DEMO xt-2 rerun --root .runs
python3 -m seqrun.cli resolve   RUN-DEMO idc-1 remap --new-barcode BC-9 --root .runs
python3 -m seqrun.cli resolve   RUN-DEMO rg-1 continue_read --refill-ml 40 --root .runs
python3 -m seqrun.cli resolve   RUN-DEMO idc-2 keep_existing --root .runs
```

## 恢复路径判定

- 标识冲突：信号未受损 → `REMAP`（重新指定唯一标识后继续）；无法确认归属
  时 `KEEP_EXISTING` 封存该样本已有结果，同区域其他样本不受影响。
- 信号串扰：污染循环尚未提交、或落在最近 2 个循环的在线补偿窗口内，
  重校准后 `CONTINUE_READ`（从首个污染循环重成像）；已提交污染循环超出窗口
  时受影响样本必须 `RERUN`。
- 试剂不足：补液后 `CONTINUE_READ`（隔离样本解隔离继续）；无法补货时
  `KEEP_EXISTING`，未完成样本封存已读部分，已完成样本不受影响。

## 时间与数据模型（确定性仿真）

- 1 仪器节拍 = 1 分钟 = 正常速度区域完成 1 个成像循环；区域速度因子 v
  通过持久化累计器决定降速/提速后的循环节拍。
- 暂停区域节拍照常流逝但不出成像事件；同批次其他区域继续；批次屏障保证
  后续批次不提前开工，暂停导致的延误向后续批次连锁顺延。
- 有效数据量 = 每百万簇每循环 2e-4 Gb × 当循环 Q30 比例；Q30 由速度、
  校准偏移、是否处于未处置串扰共同决定。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：进度与看板、区域级暂停隔离、what-if 暂停/降速、串扰的继续读取与
必须重跑、标识冲突改码、试剂不足自动隔离与补液、封存部分结果、整跑完成、
以及重启重放和事件日志篡改检测。
