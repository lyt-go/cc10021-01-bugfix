"""质量报告服务：聚合巡检和质量事件数据，生成多维度质量分析报告。

domain_chain: 质量报告 (dq_report)
  └─ 聚合 → 质量趋势 (dq_trend)
  └─ 导出 → 告警通知 (dq_alert_channel)

日报统计口径与巡检入口共用 app.services.inspection：
- 只取本轮巡检的规则明细，不把历史阶段小计累加进整轮；
- “未执行”和“执行失败”分开计数，未执行不进入通过率分母；
- 待复查清单随本轮实际执行结果回写，迟到补算刷新同一份日报；
- 人工确认过的复查数据在补算时保留。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import inspection
from app.services.base import BaseModuleService
from app.services.dq_check_run import DqCheckRunService
from app.store import store

RECHECK_DONE = "无需复查"
RECHECK_PENDING = "待复查"
RECHECK_REASON_FAILED = "执行失败"
RECHECK_REASON_NOT_RUN = "未执行"


class DqReportService(BaseModuleService):
    MODULE = "dq_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数",
        "趋势分析", "生成时间", "报告版本",
    ]
    REQUIRED_FIELDS = ["报告编号", "报告周期", "覆盖表数"]
    STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
    ACTION_RULES = {"生成报告": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
    NEGATIVE_ACTIONS = []

    def aggregate_quality_report(self, period: str = "daily",
                                 run_id: str | None = None) -> dict[str, Any]:
        """聚合本轮巡检结果和质量事件，生成/刷新多维度质量分析报告。

        run_id 不传时取最近一轮巡检。同一天同一轮次重复生成日报会刷新原记录，
        不会重复累加，也不会丢失已有的复查确认数据。
        """
        check_service = DqCheckRunService()
        stats = check_service.inspection_stats(run_id)
        round_key = stats.get("轮次标识") or stats.get("任务编号") or ""

        incident_rows = store.rows("dq_incident")
        scorecard_rows = store.rows("dq_scorecard")
        trend_rows = store.rows("dq_trend")

        # 聚合质量事件（只做展示，不参与巡检通过率）。
        total_incidents = len(incident_rows)
        open_incidents = sum(1 for r in incident_rows if r.get("status") in ("待确认", "调查中"))
        resolved_incidents = sum(1 for r in incident_rows if r.get("status") in ("已修复", "已关闭"))

        avg_quality = 0.0
        quality_values = []
        for t in trend_rows:
            try:
                quality_values.append(float(t.get("综合质量", "0")))
            except (ValueError, TypeError):
                pass
        if quality_values:
            avg_quality = sum(quality_values) / len(quality_values)

        report = self._upsert_round_report(
            period=period, round_key=round_key, stats=stats)
        dim_scores = self._compute_dimension_scores(scorecard_rows)

        return {
            "报告编号": report["报告编号"],
            "报告周期": report["报告周期"],
            "关联巡检轮次": round_key,
            # 巡检总次数按实际执行的规则数统计，与日报的通过/失败数一致。
            "巡检总次数": stats["已执行数"],
            "巡检规则总数": stats["总规则数"],
            "实际执行数": stats["已执行数"],
            "通过数": stats["通过数"],
            "失败数": stats["失败数"],
            "未执行数": stats["未执行数"],
            "跳过数": stats["跳过数"],
            "巡检通过率": stats["通过率"],
            "质量事件总数": total_incidents,
            "未处理事件": open_incidents,
            "已修复事件": resolved_incidents,
            "修复率": f"{resolved_incidents / total_incidents * 100:.1f}%" if total_incidents > 0 else "N/A",
            "平均综合质量": round(avg_quality, 2),
            "维度得分": dim_scores,
            "待复查清单": report.get("待复查清单", []),
            "可确认表": report.get("可确认表", []),
            "未出结果表": report.get("未出结果表", []),
            "生成时间": report["生成时间"],
            "趋势": report["趋势分析"],
        }

    def recheck_panel(self, report_id: int | None = None,
                      run_id: str | None = None) -> dict[str, Any]:
        """日报复查面板：按表区分“未执行”和“执行失败”。

        返回每张表本轮能否用巡检结果确认，以及需要复查的原因。
        取数直接来自巡检统计，不再对巡检任务明细逐行累加。
        """
        report = None
        stats: dict[str, Any]
        if report_id is not None:
            report = store.find(self.MODULE, report_id)
            if report is not None:
                stats = self._stats_from_report(report)
            else:
                stats = DqCheckRunService().inspection_stats(run_id)
        else:
            stats = DqCheckRunService().inspection_stats(run_id)

        table_stats = stats.get("表统计", {}) or {}
        confirmations = dict((report or {}).get("复查确认", {}))

        table_items = []
        for table, item in table_stats.items():
            reason = ""
            if item.get("未执行数", 0):
                reason = RECHECK_REASON_NOT_RUN
            elif item.get("失败数", 0):
                reason = RECHECK_REASON_FAILED
            confirmed = confirmations.get(table, {})
            table_items.append({
                **item,
                "复查原因": reason,
                "本轮结果可用": bool(item.get("可确认")),
                "确认状态": confirmed.get("确认结论") or item.get("复查状态"),
                "确认人": confirmed.get("确认人", ""),
                "确认时间": confirmed.get("确认时间", ""),
                "复查备注": confirmed.get("复查备注", ""),
            })

        return {
            "报告编号": (report or {}).get("报告编号", ""),
            "关联巡检轮次": stats.get("轮次标识") or stats.get("任务编号") or "",
            "统计": {
                "总规则数": stats.get("总规则数", 0),
                "通过数": stats.get("通过数", 0),
                "失败数": stats.get("失败数", 0),
                "未执行数": stats.get("未执行数", 0),
                "跳过数": stats.get("跳过数", 0),
                "已执行数": stats.get("已执行数", 0),
                "通过率": stats.get("通过率", "0.0%"),
            },
            "表": table_items,
            "待复查清单": [
                item["目标表名"] for item in table_items if item["复查状态"] == RECHECK_PENDING
            ],
            "可确认表": [
                item["目标表名"] for item in table_items if item["本轮结果可用"]
            ],
            "未出结果表": [
                item["目标表名"] for item in table_items if item["未执行数"] > 0
            ],
        }

    def confirm_recheck(self, report_id: int, table_name: str,
                        operator: str = "值班人员",
                        conclusion: str = "",
                        note: str = "") -> dict[str, Any] | None:
        """记录日报复查面板的人工确认。

        确认数据写在报告的 `复查确认` 上，迟到补算刷新统计时会原样保留，
        不会被历史日报的错位统计覆盖。
        """
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None
        confirmations = dict(report.get("复查确认", {}))
        panel = self.recheck_panel(report_id)
        table_item = next(
            (item for item in panel["表"] if item["目标表名"] == table_name), None)
        if table_item is None:
            return None

        if not conclusion:
            if table_item["未执行数"]:
                conclusion = RECHECK_PENDING
            elif table_item["失败数"]:
                conclusion = "需修复"
            else:
                conclusion = RECHECK_DONE

        confirmations[table_name] = {
            "确认人": operator,
            "确认时间": datetime.now(timezone.utc).isoformat(),
            "确认结论": conclusion,
            "复查备注": note,
            # 记录确认时依据的本轮统计，便于事后核对是否被旧统计带偏。
            "确认依据": {
                "未执行数": table_item["未执行数"],
                "失败数": table_item["失败数"],
                "通过数": table_item["通过数"],
                "通过率": table_item["通过率"],
                "关联巡检轮次": panel["关联巡检轮次"],
            },
        }
        report["复查确认"] = confirmations
        report["待复查清单"] = [
            table for table in panel["待复查清单"] if table != table_name
        ] if conclusion != RECHECK_PENDING else panel["待复查清单"]
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        return confirmations[table_name]

    # ---------------------------------------------------------------- 内部
    def _report_kind(self, report: dict[str, Any]) -> str:
        kind = str(report.get("报告类型") or "").strip()
        if kind:
            return kind
        period = str(report.get("报告周期") or "")
        return "巡检报告" if period.startswith("巡检-") else period

    def _stats_from_report(self, report: dict[str, Any]) -> dict[str, Any]:
        table_stats = report.get("表统计", {}) or {}
        details: list[dict[str, Any]] = []
        counter = 0

        def add(table: str, status: str, count: int) -> None:
            nonlocal counter
            for _ in range(count):
                counter += 1
                # 合成明细必须带唯一标识，否则会被巡检口径按规则编号去重成一条。
                details.append({
                    "规则编号": f"{table}#{counter}",
                    "目标表名": table,
                    "状态": status,
                })

        for table, item in table_stats.items():
            add(table, inspection.PASSED, self._as_int(item.get("通过数")))
            add(table, inspection.FAILED, self._as_int(item.get("失败数")))
            add(table, inspection.NOT_RUN, self._as_int(item.get("未执行数")))
            add(table, inspection.SKIPPED, self._as_int(item.get("跳过数")))

        if not details:
            # 历史日报只有汇总计数、没有按表明细：按旧口径还原总量，
            # 让复查面板至少能显示通过/失败/未执行，而不是一片空白。
            unscoped = "历史日报（未按表拆分）"
            add(unscoped, inspection.PASSED, self._as_int(report.get("通过规则数")))
            add(unscoped, inspection.FAILED, self._as_int(report.get("失败规则数")))
            add(unscoped, inspection.NOT_RUN, self._as_int(report.get("未执行规则数")))
            add(unscoped, inspection.SKIPPED, self._as_int(report.get("跳过规则数")))

        stats = inspection.compute_stats(details)
        stats["轮次标识"] = report.get("关联巡检轮次", "")
        stats["任务编号"] = report.get("关联巡检轮次", "")
        return stats

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return max(0, int(str(value).strip() or 0))
        except (ValueError, TypeError):
            return 0

    def _upsert_round_report(self, *, period: str, round_key: str,
                             stats: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        wanted_kind = "质量日报" if period == "daily" else period
        report = None
        for row in rows:
            if not round_key or row.get("关联巡检轮次") != round_key:
                continue
            # 只刷新日报，不覆盖巡检自动生成的巡检报告；
            # 历史数据没有“报告类型”，按报告周期前缀区分。
            if self._report_kind(row) == wanted_kind:
                report = row
                break

        values = {
            "报告类型": wanted_kind,
            "覆盖表数": str(len(stats.get("表统计", {}) or {})),
            "通过规则数": str(stats["通过数"]),
            "失败规则数": str(stats["失败数"]),
            "未执行规则数": str(stats["未执行数"]),
            "跳过规则数": str(stats["跳过数"]),
            "总规则数": str(stats["总规则数"]),
            "通过率": stats["通过率"],
            "待复查清单": list(stats.get("待复查表", [])),
            "可确认表": list(stats.get("可确认表", [])),
            "未出结果表": list(stats.get("未出结果表", [])),
            "表统计": stats.get("表统计", {}),
            "关联巡检轮次": round_key,
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "报告版本": "v1.0",
            "status": "已完成",
            "pending": stats["未执行数"] > 0,
            "abnormal": stats["失败数"] > 0,
        }

        if report is None:
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            report = {
                "id": new_id,
                "报告编号": f"DQ_R-{new_id:04d}",
                "报告周期": period,
                "趋势分析": "首次巡检",
                "复查确认": {},
            }
            rows.append(report)
        else:
            # 保留历史确认数据：补算只刷新统计和待复查清单。
            values["复查确认"] = report.get("复查确认", {})
            values["趋势分析"] = report.get("趋势分析", "首次巡检")
            values["报告周期"] = report.get("报告周期", period)

        report.update(values)
        return report

    def _compute_dimension_scores(self, scorecard_rows: list[dict]) -> dict[str, float]:
        """计算质量维度得分汇总：完整性、准确性、时效性、唯一性。"""
        dims = {"完整性": [], "准确性": [], "时效性": []}
        for s in scorecard_rows:
            for dim_key, field in [("完整性", "完整性得分"), ("准确性", "准确性得分"), ("时效性", "时效性得分")]:
                try:
                    val = float(s.get(field, "0"))
                    dims[dim_key].append(val)
                except (ValueError, TypeError):
                    pass
        return {
            k: round(sum(v) / len(v), 1) if v else 0.0
            for k, v in dims.items()
        }

    def export_report_csv(self, report_id: int) -> str | None:
        """导出质量报告为CSV格式，包含所有维度数据和趋势标记。"""
        report = store.find(self.MODULE, report_id)
        if not report:
            return None

        lines = [
            "报告编号,报告周期,覆盖表数,通过规则数,失败规则数,未执行规则数,跳过规则数,通过率,待复查清单,趋势,生成时间,版本",
            f"{report.get('报告编号')},{report.get('报告周期')},{report.get('覆盖表数')},{report.get('通过规则数')},{report.get('失败规则数')},{report.get('未执行规则数', 0)},{report.get('跳过规则数', 0)},{report.get('通过率', '')},{'|'.join(report.get('待复查清单', []) or [])},{report.get('趋势分析')},{report.get('生成时间')},{report.get('报告版本')}",
        ]
        return "\n".join(lines)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合巡检和质量事件数据生成质量分析报告"
        snapshot["artifact"] = "质量报告数据集"
        snapshot["artifact_desc"] = "聚合多维度质量指标的质量报告数据集"
        return snapshot
