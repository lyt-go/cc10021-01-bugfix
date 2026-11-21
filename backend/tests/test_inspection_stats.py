"""巡检统计与日报复查链路测试：覆盖范围外规则、并发去重、迟到补算与复跑。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dq_check_run import DqCheckRunService, PARTIAL
from app.services.dq_report import DqReportService
from app.store import store


def _add_rule(rule_id: str, table: str, rule_set: str, status: str = "已生效",
              rule_type: str = "空值检查") -> None:
    rows = store.rows("dq_rule_def")
    new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    rows.append({
        "id": new_id,
        "规则编号": rule_id,
        "规则名称": f"规则-{rule_id}",
        "目标表名": table,
        "校验字段": "amount",
        "规则类型": rule_type,
        "期望值": "0",
        "容差范围": "1",
        "规则版本": "v1",
        "规则集": rule_set,
        "status": status,
        "pending": False,
        "abnormal": False,
    })


def _reset_rounds(rule_set: str) -> None:
    store._tables["dq_check_run"] = [
        r for r in store.rows("dq_check_run") if r.get("关联规则集") != rule_set
    ]
    store._tables["dq_report"] = [
        r for r in store.rows("dq_report")
        if rule_set not in str(r.get("报告周期", "")) and rule_set not in str(r.get("关联巡检轮次", ""))
    ]


class TestInspectionPassRate:
    """巡检通过率：范围外规则不进分母，未执行不算失败。"""

    def test_out_of_scope_rule_not_in_denominator(self):
        rule_set = "TEST-SCOPE-001"
        _reset_rounds(rule_set)
        _add_rule("T-SCOPE-01", "ods_order_info", rule_set)
        _add_rule("T-SCOPE-02", "dim_unrelated_table", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-SCOPE-01", "目标表名": "ods_order_info", "状态": "通过"},
            ],
        )

        assert summary["通过数"] == 1
        assert summary["失败数"] == 0
        assert summary["未执行数"] == 0
        assert summary["跳过数"] == 1
        assert summary["总规则数"] == 1
        # 修复前这里会把范围外规则计入分母，显示 50%。
        assert summary["通过率"] == "100.0%"
        assert summary["已执行数"] == 1

    def test_not_run_and_failed_are_separate(self):
        rule_set = "TEST-SCOPE-002"
        _reset_rounds(rule_set)
        _add_rule("T-SPLIT-01", "ods_order_info", rule_set)
        _add_rule("T-SPLIT-02", "ads_sale_report", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-SPLIT-01", "目标表名": "ods_order_info", "状态": "失败"},
                # 广告表结果未出：显式标成未执行。
                {"规则编号": "T-SPLIT-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
        )

        assert summary["失败数"] == 1
        assert summary["未执行数"] == 1
        assert summary["已执行数"] == 1
        assert summary["总规则数"] == 2
        # 未执行既不作为失败，也不进入分母。
        assert summary["通过率"] == "0.0%"

    def test_concurrent_duplicate_counted_once(self):
        rule_set = "TEST-CONCURRENT-003"
        _reset_rounds(rule_set)
        _add_rule("T-CONC-01", "ods_order_info", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-CONC-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-CONC-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-CONC-01", "目标表名": "ods_order_info", "状态": "通过"},
            ],
        )

        assert summary["通过数"] == 1
        assert summary["已执行数"] == 1
        assert summary["通过率"] == "100.0%"

    def test_stage_subtotal_not_added_to_round(self):
        rule_set = "TEST-SUBTOTAL-004"
        _reset_rounds(rule_set)
        _add_rule("T-SUB-01", "ods_order_info", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-SUB-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"阶段": "订单表阶段", "小计": {"通过": 1, "失败": 0}, "规则数": 1},
            ],
        )

        assert summary["总规则数"] == 1
        assert summary["通过数"] == 1
        assert len(summary["规则明细"]) == 1

    def test_stage_subtotal_expands_children_not_added(self):
        """阶段小计只展开子明细，既不能整行相加，也不能丢掉真实结果。"""
        rule_set = "TEST-SUBTOTAL-004B"
        _reset_rounds(rule_set)
        _add_rule("T-SUB2-01", "ods_order_info", rule_set)
        _add_rule("T-SUB2-02", "ads_sale_report", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {
                    "阶段": "订单表阶段",
                    "小计": {"通过": 1, "失败": 0},
                    "规则数": 1,
                    "明细": [
                        {"规则编号": "T-SUB2-01", "目标表名": "ods_order_info", "状态": "通过"},
                    ],
                },
                {"规则编号": "T-SUB2-02", "目标表名": "ads_sale_report", "状态": "失败"},
            ],
        )

        assert summary["总规则数"] == 2
        assert summary["通过数"] == 1
        assert summary["失败数"] == 1
        # 两条规则的明细都在，且没有多出来的“小计”行。
        assert {d["规则编号"] for d in summary["规则明细"]} == {"T-SUB2-01", "T-SUB2-02"}
        assert summary["通过率"] == "50.0%"

    def test_resume_keeps_same_rule_set(self):
        """续跑只合并原轮次的规则，不把规则集之外的规则补进来。"""
        rule_set = "TEST-RESUME-005D"
        _reset_rounds(rule_set)

        svc = DqCheckRunService()
        first = svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-RS-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-RS-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
            final=False,
        )
        run_id = first["任务编号"]

        second = svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-RS-02", "目标表名": "ads_sale_report", "状态": "通过"},
            ],
            run_id=run_id,
        )

        assert second["总规则数"] == 2
        assert second["通过数"] == 2
        assert second["未执行数"] == 0
        assert second["跳过数"] == 0
        assert {d["规则编号"] for d in second["规则明细"]} == {"T-RS-01", "T-RS-02"}
        assert len([
            r for r in store.rows("dq_check_run") if r.get("任务编号") == run_id
        ]) == 1

    def test_rerun_order_table_only_is_100_percent(self):
        rule_set = "TEST-RERUN-005"
        _reset_rounds(rule_set)
        _add_rule("T-RE-01", "ods_order_info", rule_set)
        _add_rule("T-RE-02", "ads_sale_report", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-RE-01", "目标表名": "ods_order_info", "状态": "通过"},
            ],
        )

        assert summary["总规则数"] == 1
        assert summary["跳过数"] == 1
        assert summary["通过率"] == "100.0%"

    def test_explicit_results_with_missing_rule_is_not_run(self):
        """显式结果模式下，缺失的适用规则必须记未执行，不能随机补结果。"""
        rule_set = "TEST-EXPLICIT-005B"
        _reset_rounds(rule_set)
        _add_rule("T-EX-01", "ods_order_info", rule_set)
        _add_rule("T-EX-02", "ods_order_info", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-EX-01", "目标表名": "ods_order_info", "状态": "通过"},
            ],
        )

        assert summary["通过数"] == 1
        assert summary["未执行数"] == 1
        assert summary["总规则数"] == 2
        # 未执行不进分母，通过率仍按已执行的 1 条算 100%。
        assert summary["通过率"] == "100.0%"

    def test_duplicate_not_run_does_not_overwrite_terminal(self):
        rule_set = "TEST-DUPTERM-005C"
        _reset_rounds(rule_set)
        _add_rule("T-DUP-01", "ods_order_info", rule_set)

        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-DUP-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-DUP-01", "目标表名": "ods_order_info", "状态": "未执行"},
            ],
        )

        detail = summary["规则明细"][0]
        assert detail["状态"] == "通过"
        # 不能留下“结果未出”的过期原因。
        assert "原因" not in detail or detail["原因"] != "结果未出，等待迟到分区"
        assert summary["通过数"] == 1
        assert summary["未执行数"] == 0


class TestNightInspectionReport:
    """夜间巡检：订单表已出结果，广告表卡在迟到分区。"""

    def _prepare(self, rule_set: str) -> str:
        _reset_rounds(rule_set)
        _add_rule("T-NIGHT-01", "ods_order_info", rule_set)
        _add_rule("T-NIGHT-02", "ads_sale_report", rule_set, rule_type="时效性检查")
        return rule_set

    def test_report_before_and_after_late_partition(self):
        rule_set = self._prepare("TEST-NIGHT-006")
        check_svc = DqCheckRunService()
        report_svc = DqReportService()

        # 广告表尚未出结果：广告表记未执行。
        before = check_svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-NIGHT-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-NIGHT-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
            final=False,
        )
        run_id = before["任务编号"]
        assert before["未执行数"] == 1
        assert before["待复查表"] == ["ads_sale_report"]
        assert before["可确认表"] == ["ods_order_info"]

        daily = report_svc.aggregate_quality_report("daily", run_id=run_id)
        assert daily["实际执行数"] == 1
        assert daily["通过数"] == 1
        assert daily["未执行数"] == 1
        assert daily["巡检通过率"] == "100.0%"

        panel_before = report_svc.recheck_panel(daily["报告编号"] if daily.get("报告编号") else None,
                                                run_id=run_id)
        by_table = {item["目标表名"]: item for item in panel_before["表"]}
        assert by_table["ods_order_info"]["本轮结果可用"] is True
        assert by_table["ods_order_info"]["复查状态"] == "无需复查"
        assert by_table["ads_sale_report"]["本轮结果可用"] is False
        assert by_table["ads_sale_report"]["复查原因"] == "未执行"
        assert panel_before["待复查清单"] == ["ads_sale_report"]

        # 值班人员先确认订单表可以本轮结果确认。
        report_row = next(
            r for r in store.rows("dq_report")
            if r.get("关联巡检轮次") == run_id and r.get("报告类型") == "质量日报")
        confirm = report_svc.confirm_recheck(
            report_row["id"], "ods_order_info", operator="夜班", conclusion="无需复查")
        assert confirm["确认人"] == "夜班"
        assert confirm["确认依据"]["未执行数"] == 0

        # 广告表迟到分区到达，补算回写同一轮次。
        after = check_svc.record_late_results(run_id, [
            {"规则编号": "T-NIGHT-02", "目标表名": "ads_sale_report", "状态": "通过"},
        ])
        assert after["未执行数"] == 0
        assert after["通过数"] == 2
        assert after["通过率"] == "100.0%"
        assert after["待复查表"] == []
        assert after["可确认表"] == ["ods_order_info", "ads_sale_report"]

        panel_after = report_svc.recheck_panel(report_row["id"])
        by_table_after = {item["目标表名"]: item for item in panel_after["表"]}
        assert by_table_after["ads_sale_report"]["本轮结果可用"] is True
        assert by_table_after["ads_sale_report"]["未执行数"] == 0
        assert panel_after["待复查清单"] == []
        # 历史确认数据在补算后仍然保留。
        assert by_table_after["ods_order_info"]["确认人"] == "夜班"
        assert by_table_after["ods_order_info"]["确认时间"]

    def test_report_counts_match_actual_execution_after_recompute(self):
        rule_set = self._prepare("TEST-NIGHT-007")
        check_svc = DqCheckRunService()
        report_svc = DqReportService()

        before = check_svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-NIGHT-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-NIGHT-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
            final=False,
        )
        run_id = before["任务编号"]
        first = report_svc.aggregate_quality_report("daily", run_id=run_id)
        assert first["巡检总次数"] == 1
        assert first["失败数"] == 0

        check_svc.record_late_results(run_id, [
            {"规则编号": "T-NIGHT-02", "目标表名": "ads_sale_report", "状态": "失败"},
        ])
        second = report_svc.aggregate_quality_report("daily", run_id=run_id)

        # 同一轮次的日报刷新而非重复累加；巡检报告单独一份，互不覆盖。
        daily_reports = [
            r for r in store.rows("dq_report")
            if r.get("关联巡检轮次") == run_id and r.get("报告类型") == "质量日报"
        ]
        inspection_reports = [
            r for r in store.rows("dq_report")
            if r.get("关联巡检轮次") == run_id and r.get("报告类型") == "巡检报告"
        ]
        assert len(daily_reports) == 1
        assert len(inspection_reports) == 1
        assert second["巡检总次数"] == 2
        assert second["通过数"] == 1
        assert second["失败数"] == 1
        assert second["巡检通过率"] == "50.0%"
        assert daily_reports[0]["失败规则数"] == "1"
        assert daily_reports[0]["未执行规则数"] == "0"
        assert inspection_reports[0]["失败规则数"] == "1"
        assert inspection_reports[0]["未执行规则数"] == "0"

    def test_recheck_panel_distinguishes_not_run_and_failed(self):
        rule_set = "TEST-PANEL-008"
        _reset_rounds(rule_set)
        _add_rule("T-PANEL-01", "ods_order_info", rule_set)
        _add_rule("T-PANEL-02", "ads_sale_report", rule_set)

        check_svc = DqCheckRunService()
        report_svc = DqReportService()
        summary = check_svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-PANEL-01", "目标表名": "ods_order_info", "状态": "失败"},
                {"规则编号": "T-PANEL-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
            final=False,
        )
        daily = report_svc.aggregate_quality_report("daily", run_id=summary["任务编号"])
        panel = report_svc.recheck_panel(run_id=summary["任务编号"])

        by_table = {item["目标表名"]: item for item in panel["表"]}
        assert by_table["ods_order_info"]["复查原因"] == "执行失败"
        assert by_table["ads_sale_report"]["复查原因"] == "未执行"
        assert set(panel["待复查清单"]) == {"ods_order_info", "ads_sale_report"}
        assert panel["未出结果表"] == ["ads_sale_report"]

        # 巡检入口与日报面板取自同一口径。
        entry_stats = check_svc.inspection_stats(summary["任务编号"])
        assert entry_stats["失败数"] == panel["统计"]["失败数"]
        assert entry_stats["未执行数"] == panel["统计"]["未执行数"]

    def test_legacy_report_without_table_stats(self):
        """历史日报只有汇总计数、没有按表明细时，复查面板仍能给出分项统计。"""
        report_svc = DqReportService()
        rows = store.rows("dq_report")
        legacy_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": legacy_id,
            "报告编号": f"DQ_R-{legacy_id:04d}",
            "报告周期": "daily",
            "覆盖表数": "2",
            "通过规则数": "1",
            "失败规则数": "1",
            "未执行规则数": "1",
            "跳过规则数": "1",
            "通过率": "50.0%",
            "趋势分析": "平稳",
            "生成时间": "2026-11-20",
            "报告版本": "v1.0",
            "status": "已完成",
            "pending": False,
            "abnormal": True,
        })

        panel = report_svc.recheck_panel(legacy_id)
        assert panel["统计"]["通过数"] == 1
        assert panel["统计"]["失败数"] == 1
        assert panel["统计"]["未执行数"] == 1
        assert panel["统计"]["跳过数"] == 1
        assert panel["统计"]["通过率"] == "50.0%"
        assert len(panel["表"]) == 1

    def test_late_results_keep_historical_confirmation(self):
        rule_set = "TEST-HISTORY-009"
        _reset_rounds(rule_set)
        _add_rule("T-HIS-01", "ods_order_info", rule_set)
        _add_rule("T-HIS-02", "ads_sale_report", rule_set)

        check_svc = DqCheckRunService()
        report_svc = DqReportService()
        summary = check_svc.schedule_inspection(
            rule_set, "订单表,广告表",
            rule_results=[
                {"规则编号": "T-HIS-01", "目标表名": "ods_order_info", "状态": "通过"},
                {"规则编号": "T-HIS-02", "目标表名": "ads_sale_report", "状态": "未执行"},
            ],
            final=False,
        )
        run_id = summary["任务编号"]
        report_svc.aggregate_quality_report("daily", run_id=run_id)
        report_row = next(
            r for r in store.rows("dq_report")
            if r.get("关联巡检轮次") == run_id and r.get("报告类型") == "质量日报")
        report_svc.confirm_recheck(report_row["id"], "ads_sale_report",
                                   operator="值班员A", conclusion="待复查",
                                   note="等广告表分区")

        # 补算两次（重复上报），不重复计数也不丢确认。
        check_svc.record_late_results(run_id, [
            {"规则编号": "T-HIS-02", "目标表名": "ads_sale_report", "状态": "通过"},
            {"规则编号": "T-HIS-02", "目标表名": "ads_sale_report", "状态": "通过"},
        ])
        after = check_svc.inspection_stats(run_id)
        assert after["通过数"] == 2
        assert after["已执行数"] == 2
        assert after["通过率"] == "100.0%"

        history = report_row.get("复查确认", {})
        assert history["ads_sale_report"]["确认人"] == "值班员A"
        assert history["ads_sale_report"]["复查备注"] == "等广告表分区"


class TestInspectionStatsEntry:
    """巡检入口统计取数。"""

    def test_stats_excludes_seed_non_round_rows(self):
        svc = DqCheckRunService()
        # 种子数据里的巡检任务不是执行轮次，不能被当作巡检明细。
        stats = svc.inspection_stats("DQ_C-0001")
        assert stats["总规则数"] == 0
        assert stats["规则明细"] == []

    def test_latest_run_returns_latest_round(self):
        rule_set = "TEST-LATEST-010"
        _reset_rounds(rule_set)
        _add_rule("T-LAT-01", "ods_order_info", rule_set)
        svc = DqCheckRunService()
        summary = svc.schedule_inspection(
            rule_set, "订单表",
            rule_results=[
                {"规则编号": "T-LAT-01", "目标表名": "ods_order_info", "状态": "通过"},
            ],
        )
        latest = svc.latest_run()
        assert latest["任务编号"] == summary["任务编号"]
        assert latest.get("完成状态") != PARTIAL or summary["未执行数"] == 0
