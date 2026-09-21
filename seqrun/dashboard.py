"""统一呈现：样本位置/阶段、成像周期、信号质量、试剂消耗、读取进度。

提供文本看板与单文件 HTML 导出；数据全部来自事件重放后的状态。
"""
from __future__ import annotations

import json

from .impact import data_summary, full_report
from .model import FaultState, Run, Stage
from .recovery import recommend


STAGE_CN = {
    Stage.LOADING: "进样",
    Stage.CLUSTERING: "簇生成",
    Stage.IMAGING: "成像",
    Stage.BASE_CALLING: "碱基判读",
    Stage.COMPLETE: "完成",
    Stage.ISOLATED: "隔离",
}


def _stage_label(sample) -> str:
    label = STAGE_CN.get(sample.stage, sample.stage.value)
    if sample.paused:
        label += "(暂停)"
    return label


def _bar(done: int, total: int, width: int = 20) -> str:
    ratio = done / total if total else 0.0
    filled = int(ratio * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render_text(run: Run) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("测序运行 %s  状态=%s  仪器时间=%.1f min"
                 % (run.run_id, run.state.value, run.clock_min))
    lines.append("=" * 78)

    # 样本总表
    lines.append("")
    lines.append("样本位置 / 阶段 / 成像周期 / 信号质量 / 读取进度")
    lines.append("-" * 78)
    header = "%-10s %-14s %-12s %-10s %-8s %-22s %s" % (
        "样本", "位置(lane/tile)", "区域", "阶段", "Q30", "进度", "有效Gb")
    lines.append(header)
    for bid in run.batch_order:
        for rid in run.batches[bid].region_ids:
            region = run.regions[rid]
            for s in run.samples_of_region(rid):
                done, total = s.read_progress(run.reads)
                pos = "%s/%s" % (s.position.lane, s.position.tile)
                cycle_info = "%s c%s" % (
                    s.current_read or "-", s.current_cycle)
                q30 = "%.3f" % s.avg_q30() if s.qualities else "-"
                lines.append("%-10s %-14s %-12s %-12s %-8s %-22s %.3f" % (
                    s.sample_id, pos, region.name,
                    _stage_label(s) + " " + cycle_info,
                    q30,
                    "%s %d/%d" % (_bar(done, total), done, total),
                    s.effective_gb))

    # 试剂
    lines.append("")
    lines.append("试剂消耗")
    lines.append("-" * 78)
    for name, rg in run.reagents.items():
        lines.append("  %-12s 剩余 %8.2f / %8.2f ml  %s  (%.1f%%)" % (
            name, rg.remaining_ml, rg.capacity_ml,
            _bar(int(rg.fraction * 20), 20, 20), rg.fraction * 100))

    # 数据汇总
    summary = data_summary(run)
    lines.append("")
    lines.append("有效数据：已得 %.3f Gb | 预计最终 %.3f Gb | 封存风险 %.3f Gb"
                 % (summary["actual_gb"], summary["projected_final_gb"],
                    summary["at_risk_gb"]))

    # 故障与恢复建议
    open_faults = run.open_faults()
    lines.append("")
    lines.append("故障隔离与恢复路径（开放 %d 条）" % len(open_faults))
    lines.append("-" * 78)
    if not open_faults:
        lines.append("  无开放故障")
    for f in open_faults:
        rec = recommend(run, f)
        lines.append("  [%s] %s" % (f.fault_id, f.message))
        lines.append("     范围=%s 受影响=%s"
                     % (f.scope, ",".join(f.sample_ids)))
        lines.append("     建议路径=%s  备选=%s"
                     % (rec["action"], "/".join(rec.get("alternatives", []))))
        lines.append("     依据：%s" % rec["reason"])
    resolved = [f for f in run.faults.values()
                if f.state == FaultState.RESOLVED]
    if resolved:
        lines.append("  已处置：")
        for f in resolved:
            lines.append("    [%s] %s -> %s @%.1fmin"
                         % (f.fault_id, f.kind.value,
                            (f.recovered_by.value if f.recovered_by else "?"),
                            f.resolved_at_min or 0.0))
    return "\n".join(lines)


def render_html(run: Run) -> str:
    report = full_report(run)
    rows = []
    for bid in run.batch_order:
        for rid in run.batches[bid].region_ids:
            region = run.regions[rid]
            for s in run.samples_of_region(rid):
                done, total = s.read_progress(run.reads)
                pct = int(100 * done / total) if total else 0
                rows.append({
                    "sample": s.sample_id,
                    "batch": bid, "region": region.name,
                    "lane": s.position.lane, "tile": s.position.tile,
                    "x": s.position.x, "y": s.position.y,
                    "stage": _stage_label(s),
                    "read": s.current_read or "-",
                    "cycle": s.current_cycle,
                    "q30": round(s.avg_q30(), 4) if s.qualities else None,
                    "progress_pct": pct,
                    "gb": round(s.effective_gb, 3),
                    "isolated": s.stage == Stage.ISOLATED,
                    "paused": s.paused,
                })
    payload = json.dumps(
        {"report": report, "rows": rows}, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__DATA__", payload)


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>测序运行看板</title>
<style>
 body{font-family:-apple-system,"PingFang SC",sans-serif;margin:24px;color:#1f2937}
 h1{font-size:20px} .cards{display:flex;gap:12px;margin:16px 0}
 .card{border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;min-width:150px}
 .card .v{font-size:22px;font-weight:600}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}
 th{background:#f9fafb}
 .bar{background:#e5e7eb;height:8px;border-radius:4px;overflow:hidden;width:120px}
 .bar>i{display:block;height:100%;background:#2563eb}
 .iso{background:#fef3c7} .pause{color:#b45309}
 .fault{border-left:4px solid #dc2626;padding:8px 12px;margin:8px 0;background:#fef2f2}
 .ok{color:#16a34a}
</style></head><body>
<h1>测序运行统一看板</h1>
<div id="app">加载中…</div>
<script>
const DATA = __DATA__;
const r = DATA.report, rows = DATA.rows;
const app = document.getElementById('app');
app.innerHTML = `
 <div class="cards">
  <div class="card"><div>运行</div><div class="v">${r.run_id}</div>
      <div>${r.state} · ${r.clock_min} min</div></div>
  <div class="card"><div>已得有效数据</div><div class="v">${r.data.actual_gb}</div><div>Gb</div></div>
  <div class="card"><div>预计最终</div><div class="v">${r.data.projected_final_gb}</div><div>Gb</div></div>
  <div class="card"><div>封存风险</div><div class="v">${r.data.at_risk_gb}</div><div>Gb</div></div>
  <div class="card"><div>开放故障</div><div class="v">${r.open_faults.length}</div><div>条</div></div>
 </div>
 <h2>样本</h2>
 <table><thead><tr>
  <th>样本</th><th>批次</th><th>区域</th><th>位置(lane/tile/x/y)</th>
  <th>阶段</th><th>成像(读段/循环)</th><th>Q30</th><th>读取进度</th><th>有效Gb</th>
 </tr></thead><tbody>
 ${rows.map(s=>`<tr class="${s.iso?'iso':''}">
   <td>${s.sample}${s.paused?' <span class="pause">⏸</span>':''}
       ${s.iso?' <span title="隔离">⚠</span>':''}</td>
   <td>${s.batch}</td><td>${s.region}</td>
   <td>${s.lane}/${s.tile}/${s.x}/${s.y}</td>
   <td>${s.stage}</td><td>${s.read} / ${s.cycle}</td>
   <td>${s.q30==null?'-':s.q30}</td>
   <td><div class="bar"><i style="width:${s.progress_pct}%"></i></div>${s.progress_pct}%</td>
   <td>${s.gb}</td></tr>`).join('')}
 </tbody></table>
 <h2>批次排程</h2>
 <table><thead><tr><th>批次</th><th>计划开始</th><th>预计开始</th>
   <th>预计结束</th><th>延误(min)</th><th>速度</th><th>循环</th></tr></thead>
 <tbody>${r.schedule.map(b=>`<tr>
   <td>${b.batch_id}</td><td>${b.planned_start_min}</td>
   <td>${b.projected_start_min}</td><td>${b.projected_finish_min}</td>
   <td>${b.delay_min}</td><td>${b.speed_factor}x</td>
   <td>${b.cycles_done}/${b.cycles_total}</td></tr>`).join('')}</tbody></table>
`;
</script></body></html>"""
