"""巡检任务服务：调度和执行批量质量规则校验，聚合结果驱动事件和报告。

domain_chain: 巡检任务 (dq_check_run)
  └─ 调度 → 质量事件 (dq_incident)
  └─ 聚合 → 质量报告 (dq_report)
  └─ 关联 → 告警通知 (dq_alert_channel)

统计口径统一由 app.services.inspection 提供：
- 范围外规则只记“跳过”，不进入通过率分母；
- 通过率 = 通过 / (通过 + 失败)，未执行既不算通过也不算失败；
- 并发上报的同一条规则按规则编号只保留一条；
- 迟到分区补算合并回同一巡检轮次，不新增重复轮次。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services import inspection
from app.services.base import BaseModuleService
from app.store import store

RUN_RECORD_TYPE = "巡检轮次"
PARTIAL = "部分完成"
COMPLETE = "已完成"


class DqCheckRunService(BaseModuleService):
    MODULE = "dq_check_run"
    ENTITY = "巡检任务"
    OPERATION_TERM = "调度"
    LIST_FIELDS = [
        "任务编号", "关联规则集", "数据范围", "Cron表达式", "并行度",
        "超时时间", "失败阈值", "任务版本",
    ]
    REQUIRED_FIELDS = ["任务编号", "关联规则集", "数据范围"]
    STATUS_ORDER = ["草稿", "待校验", "运行中", "已暂停"]
    ACTION_RULES = {"提交校验": "待校验", "启动巡检": "运行中", "暂停巡检": "已暂停"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 调度
    def schedule_inspection(self, rule_set_id: str, data_scope: str,
                            cron_expr: str = "0 6 * * *",
                            rule_results: list[dict[str, Any]] | None = None,
                            run_id: str | None = None,
                            final: bool = True) -> dict[str, Any]:
        """调度巡检任务并执行批量规则校验。

        参数：
        - rule_results：显式给出一条或多条规则执行明细。不传时按原有方式模拟执行。
          传了明细但某条适用规则没有结果，且 final=False 时记为“未执行”，
          用于夜间巡检中广告表卡在迟到分区的场景。
        - run_id：续跑/补算时指定已有巡检轮次，结果合并回原轮次。
        - final：本次调用是否代表该轮巡检收尾。False 时缺失结果的适用规则记“未执行”；
          完成状态由未执行数决定，与阶段小计无关。

        范围外的规则写“跳过”明细，但不进入巡检总数和通过率分母。

        续跑（传 run_id）时以原轮次已有的规则为准，不会因为规则集回退
        把本轮之外的规则补进来；原轮次的跳过明细也原样保留。
        """
        existing_run = self._find_run(run_id) if run_id else None
        details: list[dict[str, Any]] = []
        provided = self._provided_results_by_key(rule_results)
        # 显式明细模式：调用方给的是本轮真实执行结果，缺失的适用规则就是“未执行”，
        # 不能再随机模拟，否则日报会对不上实际执行数。
        explicit_mode = rule_results is not None

        if existing_run is not None:
            rules = self._rules_from_run(existing_run)
        elif explicit_mode:
            rules = self._candidate_rules(rule_set_id, seed_rules=list(provided.values()))
        else:
            rules = self._candidate_rules(rule_set_id)

        # 适用规则先落明细：有显式结果用结果，否则按 final 模拟或记未执行。
        for rule in rules:
            table = str(rule.get("目标表名", "")).strip()
            key = inspection.rule_key(rule)
            base = {
                "规则编号": rule.get("规则编号"),
                "规则名称": rule.get("规则名称"),
                "规则类型": rule.get("规则类型"),
                "目标表名": table,
                "校验字段": rule.get("校验字段"),
            }
            if not inspection.in_scope(table, data_scope):
                details.append({
                    **base,
                    "状态": inspection.SKIPPED,
                    "执行状态": inspection.SKIPPED,
                    "跳过原因": "规则目标表不在本次巡检数据范围内",
                })
                continue
            if key in provided:
                detail = {**base, **provided.pop(key)}
                detail["目标表名"] = detail.get("目标表名") or table
                details.append(detail)
                continue
            if explicit_mode or not final:
                details.append({
                    **base,
                    "状态": inspection.NOT_RUN,
                    "执行状态": inspection.NOT_RUN,
                    "原因": "结果未出，等待迟到分区",
                })
                continue
            details.append({**base, **self._simulate_rule_result(base)})

        # 显式结果中另外出现的规则：在范围内则计入，范围外仍只记跳过。
        for leftover in provided.values():
            table = str(leftover.get("目标表名") or leftover.get("table_name") or "").strip()
            detail = inspection.normalize_detail(leftover)
            if not inspection.in_scope(table, data_scope):
                detail["状态"] = inspection.SKIPPED
                detail["执行状态"] = inspection.SKIPPED
                detail["跳过原因"] = "规则目标表不在本次巡检数据范围内"
            details.append(detail)

        details = self._dedupe_concurrent(details)
        stats = inspection.compute_stats(details)

        task_id = run_id or self._new_task_id()
        run = self._upsert_run(
            task_id=task_id,
            rule_set_id=rule_set_id,
            data_scope=data_scope,
            cron_expr=cron_expr,
            stats=stats,
            incoming_details=details,
            final=final,
        )

        failures = [d for d in run.get("规则明细", [])
                    if d.get("状态") == inspection.FAILED]
        if failures:
            self._write_quality_incidents(run["任务编号"], failures)

        summary = self._summary_from_run(run)
        self._generate_inspection_report(summary, run)
        return summary

    def record_late_results(self, run_id: str,
                            rule_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """迟到分区补算：把后到的规则结果合并回同一巡检轮次。

        并发的重复结果按规则编号去重；已确认的规则不会被重复计一次。
        合并后会重算整轮统计并刷新关联的巡检报告，历史日报的确认数据保留。
        """
        run = self._find_run(run_id)
        if run is None:
            return None

        incoming = []
        for raw in rule_results or []:
            detail = inspection.normalize_detail(raw)
            table = str(detail.get("目标表名") or detail.get("table_name") or "")
            if table and not inspection.in_scope(table, run.get("数据范围", "")):
                detail["状态"] = inspection.SKIPPED
                detail["执行状态"] = inspection.SKIPPED
                detail["跳过原因"] = "规则目标表不在本次巡检数据范围内"
            incoming.append(detail)

        merged = inspection.merge_rule_details(run.get("规则明细", []), incoming)
        stats = inspection.compute_stats(merged)
        run.update(inspection.stats_as_legacy_fields(stats))
        run.update({
            "规则明细": stats["规则明细"],
            "表统计": stats["表统计"],
            "待复查表": stats["待复查表"],
            "可确认表": stats["可确认表"],
            "未出结果表": stats["未出结果表"],
            "执行时间": datetime.now(timezone.utc).isoformat(),
            "完成状态": COMPLETE if stats["未执行数"] == 0 else PARTIAL,
            "pending": stats["未执行数"] > 0,
            "abnormal": stats["失败数"] > 0,
        })

        failures = [d for d in run["规则明细"] if d.get("状态") == inspection.FAILED]
        if failures:
            self._write_quality_incidents(run["任务编号"], failures)

        summary = self._summary_from_run(run)
        self._generate_inspection_report(summary, run)
        return summary

    # -------------------------------------------------------------- 统计取数
    def inspection_stats(self, task_id: str | None = None) -> dict[str, Any]:
        """巡检入口取数：返回区分通过、失败、跳过、未执行的巡检统计。"""
        run = self._find_run(task_id) if task_id else self.latest_run()
        if run is None:
            return {
                "任务编号": "",
                "总规则数": 0,
                "通过数": 0,
                "失败数": 0,
                "跳过数": 0,
                "未执行数": 0,
                "已执行数": 0,
                "通过率": "0.0%",
                "规则明细": [],
                "表统计": {},
                "待复查表": [],
                "可确认表": [],
                "未出结果表": [],
                "完成状态": PARTIAL,
            }
        return self._summary_from_run(run)

    def latest_run(self) -> dict[str, Any] | None:
        """取最近一轮巡检轮次；阶段小计行不算轮次。"""
        rounds = [
            row for row in store.rows(self.MODULE)
            if self._is_round(row)
        ]
        if not rounds:
            return None
        return max(rounds, key=lambda row: (
            int(row.get("id", 0)), str(row.get("执行时间", "")),
        ))

    # ---------------------------------------------------------------- 内部
    def _candidate_rules(self, rule_set_id: str,
                         seed_rules: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """挑出本次巡检适用的规则定义。

        规则集没有独立映射表时沿用历史行为：取已生效/待校验规则。
        如果 rule_set_id 精确命中某条规则或规则的规则集字段，则收窄范围。
        显式给了执行明细时，规则集未命中任何定义就以明细自身为准，
        避免把规则集之外的规则补进本轮统计。
        """
        rule_rows = list(store.rows("dq_rule_def"))
        active = [r for r in rule_rows if r.get("status") in ("已生效", "待校验")]

        narrowed = [
            r for r in active
            if str(r.get("规则编号", "")) == str(rule_set_id)
            or str(r.get("规则集", "")) == str(rule_set_id)
            or str(r.get("关联规则集", "")) == str(rule_set_id)
        ]
        if narrowed:
            return narrowed
        if seed_rules is not None:
            # 显式结果模式下，只统计调用方实际给出的规则。
            return list(seed_rules)
        return active

    def _rules_from_run(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """从已有轮次的明细还原规则清单，用于续跑时保持同一批规则。"""
        rules = []
        for detail in run.get("规则明细", []) or []:
            rules.append({
                "规则编号": detail.get("规则编号"),
                "规则名称": detail.get("规则名称"),
                "规则类型": detail.get("规则类型"),
                "目标表名": detail.get("目标表名"),
                "校验字段": detail.get("校验字段"),
            })
        return rules

    def _provided_results_by_key(
        self, rule_results: list[dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        """收拢显式结果：先按并发合并口径去重，终态不会被迟到的未执行覆盖。"""
        provided: dict[str, dict[str, Any]] = {}
        for detail in inspection.merge_rule_details([], rule_results or []):
            key = inspection.rule_key(detail)
            if key:
                provided[key] = detail
        return provided

    def _simulate_rule_result(self, base: dict[str, Any]) -> dict[str, Any]:
        passed = random.random() > 0.2
        result = {
            "状态": inspection.PASSED if passed else inspection.FAILED,
            "执行状态": inspection.PASSED if passed else inspection.FAILED,
            "执行时间": datetime.now(timezone.utc).isoformat(),
        }
        if not passed:
            result["失败原因"] = f"{base.get('规则类型') or '规则'}校验未通过"
        return result

    def _dedupe_concurrent(self, details: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """并发 worker 上报的同一规则只保留一条，终态优先。"""
        return inspection.merge_rule_details([], details)

    def _new_task_id(self) -> str:
        return f"DQ_C-inspect-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"

    def _is_round(self, row: dict[str, Any]) -> bool:
        return row.get("记录类型") == RUN_RECORD_TYPE or "规则明细" in row

    def _find_run(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        for row in store.rows(self.MODULE):
            if not self._is_round(row):
                continue
            if str(row.get("任务编号", "")) == str(task_id) or str(row.get("id")) == str(task_id):
                return row
        return None

    def _upsert_run(self, *, task_id: str, rule_set_id: str, data_scope: str,
                    cron_expr: str, stats: dict[str, Any],
                    incoming_details: list[dict[str, Any]],
                    final: bool) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        run = self._find_run(task_id)
        if run is None:
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            run = {
                "id": new_id,
                "任务编号": task_id,
                "轮次标识": task_id,
                "记录类型": RUN_RECORD_TYPE,
                "关联规则集": rule_set_id,
                "数据范围": data_scope,
                "Cron表达式": cron_expr,
                "并行度": "4",
                "超时时间": "3600s",
                "失败阈值": "20%",
                "任务版本": "v1",
                "status": "运行中",
                "pending": True,
                "abnormal": False,
                "规则明细": [],
                "表统计": {},
                "复查确认": {},
            }
            rows.append(run)
        else:
            # 续跑时保留已有明细，合并而不是覆盖，避免补算丢结果。
            merged = inspection.merge_rule_details(
                run.get("规则明细", []), incoming_details)
            stats = inspection.compute_stats(merged)

        run.update(inspection.stats_as_legacy_fields(stats))
        run.update({
            "关联规则集": rule_set_id or run.get("关联规则集", ""),
            "数据范围": data_scope or run.get("数据范围", ""),
            "Cron表达式": cron_expr or run.get("Cron表达式", "0 6 * * *"),
            "规则明细": stats["规则明细"],
            "表统计": stats["表统计"],
            "待复查表": stats["待复查表"],
            "可确认表": stats["可确认表"],
            "未出结果表": stats["未出结果表"],
            "完成状态": COMPLETE if stats["未执行数"] == 0 else PARTIAL,
            "执行时间": datetime.now(timezone.utc).isoformat(),
            "pending": stats["未执行数"] > 0,
            "abnormal": stats["失败数"] > 0,
        })
        return run

    def _summary_from_run(self, run: dict[str, Any]) -> dict[str, Any]:
        details = run.get("规则明细", [])
        stats = inspection.compute_stats(details)
        return {
            "任务编号": run.get("任务编号"),
            "轮次标识": run.get("轮次标识") or run.get("任务编号"),
            "关联规则集": run.get("关联规则集"),
            "数据范围": run.get("数据范围"),
            "总规则数": stats["总规则数"],
            "规则总数": stats["规则总数"],
            "适用规则数": stats["适用规则数"],
            "范围外规则数": stats["范围外规则数"],
            "通过数": stats["通过数"],
            "失败数": stats["失败数"],
            "执行失败数": stats["执行失败数"],
            "跳过数": stats["跳过数"],
            "未执行数": stats["未执行数"],
            "已执行数": stats["已执行数"],
            "实际执行数": stats["实际执行数"],
            "通过率": stats["通过率"],
            "巡检通过率": stats["巡检通过率"],
            "完成状态": run.get("完成状态", PARTIAL),
            "执行时间": run.get("执行时间"),
            "Cron表达式": run.get("Cron表达式"),
            "规则明细": stats["规则明细"],
            "表统计": stats["表统计"],
            "待复查表": stats["待复查表"],
            "可确认表": stats["可确认表"],
            "未出结果表": stats["未出结果表"],
        }

    def _write_quality_incidents(self, task_id: str, failures: list[dict]) -> None:
        """写入质量事件记录：将巡检失败的规则关联到质量事件管理。"""
        incident_rows = store.rows("dq_incident")
        existing_keys = {
            (row.get("关联规则"), row.get("巡检轮次"))
            for row in incident_rows
        }
        for fail in failures:
            identity = (fail.get("规则编号", ""), task_id)
            if identity in existing_keys:
                continue
            new_iid = max((int(r.get("id", 0)) for r in incident_rows), default=0) + 1
            incident_rows.append({
                "id": new_iid,
                "事件编号": f"DQ_I-{new_iid:04d}",
                "关联规则": fail.get("规则编号", ""),
                "巡检轮次": task_id,
                "目标表名": fail.get("目标表名", ""),
                "触发时间": datetime.now(timezone.utc).isoformat(),
                "偏离程度": "100%",
                "影响行数": "待评估",
                "严重级别": "warning",
                "处理状态": "自动生成",
                "处理人": "巡检系统",
                "status": "待确认",
                "pending": True,
                "abnormal": True,
            })
            existing_keys.add(identity)

    def _generate_inspection_report(self, summary: dict[str, Any],
                                    run: dict[str, Any]) -> None:
        """把巡检结果写入巡检报告，并同步刷新同轮次已生成的日报。

        - 巡检报告与值班人员生成的质量日报分开存，避免互相覆盖；
        - 同一巡检轮次只保留一份巡检报告，迟到补算刷新而不是追加新行；
        - 若该轮次已生成过质量日报，补算后同步刷新日报统计和待复查清单，
          但人工确认过的 `复查确认` 原样保留。
        """
        report_rows = store.rows("dq_report")
        round_key = summary.get("轮次标识") or summary.get("任务编号")

        values = {
            "报告类型": "巡检报告",
            "报告周期": f"巡检-{summary['任务编号']}",
            "覆盖表数": str(len(summary.get("表统计", {}) or {})),
            "通过规则数": str(summary["通过数"]),
            "失败规则数": str(summary["失败数"]),
            "未执行规则数": str(summary["未执行数"]),
            "跳过规则数": str(summary["跳过数"]),
            "总规则数": str(summary["总规则数"]),
            "通过率": summary["通过率"],
            "待复查清单": list(summary.get("待复查表", [])),
            "可确认表": list(summary.get("可确认表", [])),
            "未出结果表": list(summary.get("未出结果表", [])),
            "表统计": summary.get("表统计", {}),
            "关联巡检轮次": round_key,
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "报告版本": "v1",
            "status": "已完成",
            "pending": summary["未执行数"] > 0,
            "abnormal": summary["失败数"] > 0,
        }

        inspection_report = None
        daily_report = None
        for row in report_rows:
            if row.get("关联巡检轮次") != round_key:
                continue
            kind = str(row.get("报告类型") or "")
            if kind == "质量日报":
                daily_report = row
            elif inspection_report is None:
                inspection_report = row

        if inspection_report is None:
            new_rid = max((int(r.get("id", 0)) for r in report_rows), default=0) + 1
            inspection_report = {
                "id": new_rid,
                "报告编号": f"DQ_R-{new_rid:04d}",
                "趋势分析": "首次巡检",
            }
            report_rows.append(inspection_report)
        else:
            values["趋势分析"] = inspection_report.get("趋势分析", "首次巡检")
        inspection_report.update(values)

        if daily_report is not None:
            # 日报只同步统计口径，确认数据不被补算覆盖。
            daily_report.update({
                "覆盖表数": values["覆盖表数"],
                "通过规则数": values["通过规则数"],
                "失败规则数": values["失败规则数"],
                "未执行规则数": values["未执行规则数"],
                "跳过规则数": values["跳过规则数"],
                "总规则数": values["总规则数"],
                "通过率": values["通过率"],
                "待复查清单": values["待复查清单"],
                "可确认表": values["可确认表"],
                "未出结果表": values["未出结果表"],
                "表统计": values["表统计"],
                "生成时间": values["生成时间"],
                "pending": values["pending"],
                "abnormal": values["abnormal"],
                "复查确认": daily_report.get("复查确认", {}),
            })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "调度"
        snapshot["desc"] = "调度巡检任务并执行批量规则校验"
        snapshot["artifact"] = "巡检任务执行记录"
        snapshot["artifact_desc"] = "聚合巡检规则执行结果的巡检任务执行记录"
        return snapshot
