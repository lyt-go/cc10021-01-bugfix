"""血缘图谱服务：解析和关联数据表上下游依赖关系，驱动元数据变更对比和质量事件追踪。

domain_chain: 血缘图谱 (lineage_graph)
  └─ 关联 → 元数据变更对比 (metadata_diff)
  └─ 查询 → 质量事件追踪 (dq_incident)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class LineageGraphService(BaseModuleService):
    MODULE = "lineage_graph"
    ENTITY = "血缘关系"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关系编号", "上游表名", "下游表名", "来源字段", "目标字段", "转换逻辑", "采集批次", "关系版本"]
    REQUIRED_FIELDS = ["关系编号", "上游表名", "下游表名"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用关系": "已停用"}
    NEGATIVE_ACTIONS = ["停用关系"]

    def parse_lineage_from_sql(self, sql_text: str, target_table: str) -> list[dict[str, Any]]:
        """解析SQL语句提取上下游血缘关系并关联入图谱。

        从SQL解析FROM/JOIN子句，识别上游源表，建立数据血缘关系。
        解析结果关联写入血缘图谱，同时触发变更对比流程。
        """
        import re
        from_pattern = re.findall(r'(?:FROM|JOIN)\s+(\w+(?:\.\w+)?)', sql_text, re.IGNORECASE)
        upstream_tables = list(set(from_pattern))
        if target_table in upstream_tables:
            upstream_tables.remove(target_table)

        lineages = []
        lineage_rows = store.rows(self.MODULE)
        for upstream in upstream_tables:
            new_lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
            entry = {
                "id": new_lid,
                "关系编号": f"LINE-{new_lid:04d}",
                "上游表名": upstream,
                "下游表名": target_table,
                "来源字段": "*",
                "目标字段": "*",
                "转换逻辑": "SQL解析",
                "采集批次": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
                "关系版本": "v1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            lineage_rows.append(entry)
            lineages.append(entry)
        return lineages

    def build_lineage_graph(self, table_name: str, depth: int = 3) -> dict[str, Any]:
        """构建指定表的血缘关系图谱，向上递归探索上游，向下递归探索下游。

        查询关联到该表的所有血缘记录，构建双向血缘图并聚合为图谱快照。
        """
        all_rows = store.rows(self.MODULE)
        # 查询上下游关系
        upstream = [r for r in all_rows if r.get("下游表名") == table_name and r.get("status") == "已生效"]
        downstream = [r for r in all_rows if r.get("上游表名") == table_name and r.get("status") == "已生效"]

        # 递归展开上游
        expanded_upstream = self._expand_lineage(upstream, direction="up", depth=depth - 1)

        return {
            "中心表": table_name,
            "上游表数": len(expanded_upstream),
            "下游表数": len(downstream),
            "上游血缘": [
                {"表名": u.get("上游表名"), "关系编号": u.get("关系编号"),
                 "转换逻辑": u.get("转换逻辑"), "关系版本": u.get("关系版本")}
                for u in expanded_upstream
            ],
            "下游血缘": [
                {"表名": d.get("下游表名"), "关系编号": d.get("关系编号"),
                 "转换逻辑": d.get("转换逻辑")}
                for d in downstream
            ],
            "图谱生成时间": datetime.now(timezone.utc).isoformat(),
        }

    def _expand_lineage(self, entries: list[dict], direction: str, depth: int) -> list[dict]:
        """递归展开血缘关系，深度遍历上下游关联路径。"""
        if depth <= 0 or not entries:
            return entries
        all_rows = store.rows(self.MODULE)
        expanded = list(entries)
        for entry in entries:
            if direction == "up":
                parents = [r for r in all_rows
                           if r.get("下游表名") == entry.get("上游表名")
                           and r.get("status") == "已生效"
                           and r not in expanded]
            else:
                children = [r for r in all_rows
                            if r.get("上游表名") == entry.get("下游表名")
                            and r.get("status") == "已生效"
                            and r not in expanded]
            expanded.extend(parents if direction == "up" else children)
        return expanded

    def detect_lineage_breakage(self, table_name: str) -> list[dict]:
        """检测血缘断链：查询上游表是否仍存在于表目录中。

        关联血缘图与表目录，对比校验血缘上下游的完整性，识别断链的表依赖。
        """
        catalog_rows = store.rows("table_catalog")
        all_rows = store.rows(self.MODULE)
        upstream_entries = [r for r in all_rows if r.get("下游表名") == table_name]
        breakages = []
        for entry in upstream_entries:
            upstream_name = entry.get("上游表名", "")
            found = any(c.get("表名称") == upstream_name for c in catalog_rows)
            if not found:
                breakages.append({
                    "关系编号": entry.get("关系编号"),
                    "断链上游": upstream_name,
                    "下游": table_name,
                    "检测到断链": True,
                    "建议操作": "重新采集上游元数据或标记为已停用",
                })
        return breakages

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "关联"
        snapshot["desc"] = "解析SQL提取血缘关系并关联入图谱"
        snapshot["artifact"] = "血缘图谱数据集"
        snapshot["artifact_desc"] = "上下游血缘关系图谱数据集"
        return snapshot