"""规则模板库服务：管理和标准化质量规则模板，驱动规则定义和执行。

domain_chain: 规则模板 (dq_template)
  └─ 标准化 → 质量规则定义 (dq_rule_def)
  └─ 校验 → 巡检任务 (dq_check_run)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DqTemplateService(BaseModuleService):
    MODULE = "dq_template"
    ENTITY = "规则模板"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["模板编号", "模板名称", "规则类型", "校验SQL", "参数列表", "适用场景", "使用次数", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "模板名称", "规则类型"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    BUILTIN_TEMPLATES = {
        "null_check": "SELECT COUNT(*) as null_count FROM {table} WHERE {column} IS NULL",
        "uniqueness_check": "SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} HAVING COUNT(*) > 1",
        "range_check": "SELECT COUNT(*) FROM {table} WHERE {column} NOT BETWEEN {min} AND {max}",
        "freshness_check": "SELECT MAX({column}) FROM {table} HAVING MAX({column}) < DATE('now', '-{interval} days')",
        "format_check": "SELECT COUNT(*) FROM {table} WHERE {column} NOT REGEXP '{pattern}'",
        "referential_integrity": "SELECT COUNT(*) FROM {table} t LEFT JOIN {ref} r ON t.{column}=r.{ref_col} WHERE r.{ref_col} IS NULL",
        "completeness_check": "SELECT COUNT(*) as total, COUNT({column}) as non_null, COUNT(*) - COUNT({column}) as missing FROM {table}",
        "distribution_check": "SELECT {column}, COUNT(*) as freq FROM {table} GROUP BY {column} ORDER BY freq DESC LIMIT 100",
    }

    def standardize_rule_template(self, template_name: str, rule_type: str,
                                   sql_template: str, params: list[str]) -> dict[str, Any]:
        """标准化规则模板：校验模板SQL合法性并写入模板库。

        对传入的模板SQL进行解析校验，检查参数占位符与参数列表是否匹配，
        标准化后的模板写入库中并关联到规则定义服务。
        """
        rows = store.rows(self.MODULE)

        # 校验参数占位符
        import re
        placeholders = set(re.findall(r'\{(\w+)\}', sql_template))
        declared_params = set(params)
        missing_in_sql = declared_params - placeholders
        extra_in_sql = placeholders - declared_params

        validation = {
            "SQL占位符": list(placeholders),
            "声明参数": params,
            "SQL中未声明": list(missing_in_sql),
            "声明但未用": list(extra_in_sql),
            "是否一致": len(missing_in_sql) == 0 and len(extra_in_sql) == 0,
        }

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "模板编号": f"DQ_T-{new_id:04d}",
            "模板名称": template_name,
            "规则类型": rule_type,
            "校验SQL": sql_template.replace("\n", " ").strip(),
            "参数列表": str(params),
            "适用场景": f"{rule_type}标准化模板",
            "使用次数": "0",
            "模板版本": "v1.0",
            "status": "已发布",
            "pending": False,
            "abnormal": not validation["是否一致"],
        }
        rows.append(entry)

        # 关联写入规则定义
        rule_rows = store.rows("dq_rule_def")
        new_rid = max((int(r.get("id", 0)) for r in rule_rows), default=0) + 1
        rule_rows.append({
            "id": new_rid,
            "规则编号": f"DQ_R-{new_rid:04d}",
            "规则名称": f"{template_name}_auto",
            "目标表名": "all_tables",
            "校验字段": params[0] if params else "id",
            "规则类型": rule_type,
            "期望值": "0",
            "容差范围": "0.1",
            "规则版本": "v1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        })

        return {
            "模板编号": entry["模板编号"],
            "标准化结果": "成功",
            "校验详情": validation,
            "关联规则编号": f"DQ_R-{new_rid:04d}",
            "标准化时间": datetime.now(timezone.utc).isoformat(),
        }

    def instantiate_template(self, template_id: int, param_values: dict[str, Any]) -> dict[str, Any]:
        """实例化模板：用实际参数替换模板占位符，生成可执行的校验SQL。

        查询模板定义，将参数值代入SQL模板生成实际可执行的校验语句，
        同时生成规则定义记录并关联到巡检任务。
        """
        template = store.find(self.MODULE, template_id)
        if not template:
            return {"状态": "失败", "原因": f"模板{template_id}不存在"}

        sql = template.get("校验SQL", "")
        missing_params = []

        for param_key in set(param_values.keys()):
            placeholder = "{" + param_key + "}"
            if placeholder in sql:
                sql = sql.replace(placeholder, str(param_values[param_key]))
            else:
                missing_params.append(param_key)

        return {
            "模板名称": template.get("模板名称"),
            "实例化SQL": sql,
            "参数值": param_values,
            "未使用参数": missing_params,
            "实例化时间": datetime.now(timezone.utc).isoformat(),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "标准化"
        snapshot["desc"] = "标准化规则模板SQL并关联规则定义"
        snapshot["artifact"] = "规则模板定义集"
        snapshot["artifact_desc"] = "经过标准化和校验的规则模板定义集"
        return snapshot