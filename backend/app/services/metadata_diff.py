"""元数据变更对比服务：检测和记录表结构变更，驱动血缘图谱更新。

domain_chain: 元数据变更对比 (metadata_diff)
  └─ 校验 → 血缘图谱更新 (lineage_graph)
  └─ 关联 → 质量事件 (dq_incident)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MetadataDiffService(BaseModuleService):
    MODULE = "metadata_diff"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["变更编号", "变更对象", "变更类型", "旧值", "新值", "变更批次", "影响评估", "变更时间"]
    REQUIRED_FIELDS = ["变更编号", "变更对象", "变更类型"]
    STATUS_ORDER = ["草稿", "待审核", "已确认", "已回滚"]
    ACTION_RULES = {"提交审核": "待审核", "确认变更": "已确认", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def compare_schemas(self, old_columns: list[dict], new_columns: list[dict],
                         table_name: str) -> dict[str, Any]:
        """对比两次元数据采集的表结构差异，生成变更记录。

        解析新老字段列表，识别新增、删除、类型变更等差异，
        校验变更影响范围并写入变更记录，触发血缘图谱的更新。
        """
        old_map = {c.get("字段名称"): c for c in old_columns if c.get("字段名称")}
        new_map = {c.get("字段名称"): c for c in new_columns if c.get("字段名称")}
        old_names = set(old_map.keys())
        new_names = set(new_map.keys())

        additions = new_names - old_names
        deletions = old_names - new_names
        common = old_names & new_names

        changes = []
        # 识别新增字段
        for col_name in additions:
            changes.append({"字段": col_name, "变更类型": "新增",
                            "旧值": "N/A",
                            "新值": f"类型:{new_map[col_name].get('数据类型', 'unknown')}"})
        # 识别删除字段
        for col_name in deletions:
            changes.append({"字段": col_name, "变更类型": "删除",
                            "旧值": f"类型:{old_map[col_name].get('数据类型', 'unknown')}",
                            "新值": "N/A"})
        # 识别类型变更
        for col_name in common:
            old_type = old_map[col_name].get("数据类型", "")
            new_type = new_map[col_name].get("数据类型", "")
            if old_type != new_type:
                changes.append({"字段": col_name, "变更类型": "类型变更",
                                "旧值": old_type, "新值": new_type})

        batch_hash = hashlib.md5(f"{table_name}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]

        # 写入变更记录
        diff_rows = store.rows(self.MODULE)
        for change in changes:
            new_did = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
            diff_rows.append({
                "id": new_did,
                "变更编号": f"META-{new_did:04d}",
                "变更对象": f"{table_name}.{change['字段']}",
                "变更类型": change["变更类型"],
                "旧值": change["旧值"],
                "新值": change["新值"],
                "变更批次": batch_hash,
                "影响评估": self._assess_impact(change["变更类型"], table_name, change["字段"]),
                "变更时间": datetime.now(timezone.utc).isoformat(),
                "status": "已确认" if change["变更类型"] != "删除" else "待审核",
                "pending": change["变更类型"] == "删除",
                "abnormal": change["变更类型"] == "删除",
            })

        # 检测到字段删除：校验血缘完整性并触发更新
        if deletions:
            self._validate_lineage_impact(table_name, list(deletions))

        return {
            "表名称": table_name,
            "旧字段数": len(old_columns),
            "新字段数": len(new_columns),
            "变更总数": len(changes),
            "新增字段": list(additions),
            "删除字段": list(deletions),
            "类型变更": [c for c in changes if c["变更类型"] == "类型变更"],
            "变更批次": batch_hash,
        }

    def _assess_impact(self, change_type: str, table_name: str, field_name: str) -> str:
        """评估变更影响范围。"""
        if change_type == "删除":
            return f"高风险：可能影响下游依赖{table_name}的表，需校验血缘完整性"
        elif change_type == "类型变更":
            return f"中风险：可能影响ETL转换逻辑和数据写入"
        else:
            return "低风险：新增字段不影响已有逻辑"

    def _validate_lineage_impact(self, table_name: str, deleted_fields: list[str]) -> None:
        """校验血缘完整性：检测删除字段是否影响下游表依赖。"""
        lineage_rows = store.rows("lineage_graph")
        affected = [
            r for r in lineage_rows
            if r.get("上游表名") == table_name and r.get("status") == "已生效"
        ]
        for lr in affected:
            lr["abnormal"] = True
            lr["status"] = "待校验"

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "校验"
        snapshot["desc"] = "对比元数据schema差异并校验血缘影响"
        snapshot["artifact"] = "变更对比记录数据集"
        snapshot["artifact_desc"] = "含影响评估的变更对比记录数据集"
        return snapshot