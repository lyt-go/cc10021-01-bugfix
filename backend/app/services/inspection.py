"""巡检统计公共口径：规则明细归一、并发去重、范围过滤和通过率计算。

巡检入口（dq_check_run / dq_rule_def）和日报复查面板（dq_report）都从这里取数，
避免两个入口各自按 `abnormal` 之类的粗粒度字段统计，出现“未执行”和“失败”混算、
阶段小计与整轮重复相加的问题。

统计口径：
- 通过 / 失败 / 跳过 / 未执行 四类明细互斥；
- 范围外规则只记“跳过”，不进入通过率分母；
- 通过率 = 通过数 / (通过数 + 失败数)；
- 并发上报的同一条规则按规则编号只保留一条；
- 明细里带“小计”标记的阶段汇总不参与整轮统计。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable

PASSED = "通过"
FAILED = "失败"
SKIPPED = "跳过"
NOT_RUN = "未执行"

TERMINAL_STATUSES = {PASSED, FAILED}
ALL_STATUSES = {PASSED, FAILED, SKIPPED, NOT_RUN}
STATUS_ORDER = [PASSED, FAILED, NOT_RUN, SKIPPED]

# 状态别名：既有后端历史字段（abnormal / 校验结果 / 执行状态），也兼容英文值。
_STATUS_ALIASES = {
    "通过": PASSED,
    "passed": PASSED,
    "pass": PASSED,
    "success": PASSED,
    "succeed": PASSED,
    "成功": PASSED,
    "正常": PASSED,
    "ok": PASSED,
    "失败": FAILED,
    "failed": FAILED,
    "fail": FAILED,
    "error": FAILED,
    "异常": FAILED,
    "错误": FAILED,
    "不通过": FAILED,
    "跳过": SKIPPED,
    "skipped": SKIPPED,
    "skip": SKIPPED,
    "范围外": SKIPPED,
    "不适用": SKIPPED,
    "不在范围": SKIPPED,
    "未执行": NOT_RUN,
    "not_run": NOT_RUN,
    "not-run": NOT_RUN,
    "pending": NOT_RUN,
    "waiting": NOT_RUN,
    "未出结果": NOT_RUN,
    "结果未出": NOT_RUN,
    "待执行": NOT_RUN,
    "迟到": NOT_RUN,
    "等待": NOT_RUN,
    "超时": NOT_RUN,
}

# 这些键可能是“阶段小计”而不应该进入整轮明细。
_SUBTOTAL_FLAGS = ("小计", "is_subtotal", "subtotal", "汇总", "阶段汇总")


def normalize_status(value: Any) -> str:
    """把各种写法归一为四种巡检状态之一，无法识别时返回空串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return PASSED if value else FAILED
    text = str(value).strip()
    if not text:
        return ""
    lowered = text.casefold()
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    if lowered in _STATUS_ALIASES:
        return _STATUS_ALIASES[lowered]
    # 兼容“校验未通过”“执行失败：xxx”“未执行（迟到分区）”这类描述。
    for word in ("未执行", "未出结果", "结果未出", "待执行", "等待结果", "迟到", "超时"):
        if word in text:
            return NOT_RUN
    for word in ("跳过", "范围外", "不适用"):
        if word in text:
            return SKIPPED
    if "未通过" in text or "失败" in text or "异常" in text:
        return FAILED
    if "通过" in text or "成功" in text:
        return PASSED
    return ""


def is_subtotal(detail: dict[str, Any]) -> bool:
    """判断明细是否只是阶段小计，避免被重复计入整轮。"""
    for flag in _SUBTOTAL_FLAGS:
        value = detail.get(flag)
        if value is True:
            return True
        if flag == "小计" and isinstance(value, dict) and value:
            return True
        if flag in ("is_subtotal", "subtotal") and str(value).strip().casefold() in (
            "1", "true", "yes", "是",
        ):
            return True
    # 只带“规则数”而没有具体规则标识的汇总行也按小计处理。
    has_rule_identity = any(
        str(detail.get(key) or "").strip()
        for key in ("规则编号", "rule_id", "规则名称", "规则类型")
    )
    if not has_rule_identity and ("规则数" in detail or "总数" in detail):
        return True
    return False


def rule_key(detail: dict[str, Any]) -> str:
    """规则明细去重键：优先规则编号，其次 rule_id / id，最后用表+规则类型。"""
    for key in ("规则编号", "rule_id", "规则编码", "规则名称"):
        value = str(detail.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    for key in ("id", "ID"):
        value = detail.get(key)
        if value not in (None, ""):
            return f"id:{value}"
    table = str(detail.get("目标表名") or detail.get("table_name") or "").strip()
    rule_type = str(detail.get("规则类型") or detail.get("rule_type") or "").strip()
    field = str(detail.get("校验字段") or "").strip()
    if table or rule_type or field:
        return f"composite:{table}|{rule_type}|{field}"
    return ""


def _detail_status(detail: dict[str, Any], default: str = "") -> str:
    for key in (
        "状态", "执行状态", "校验结果", "执行结果", "结果", "result", "status",
    ):
        if key in detail:
            status = normalize_status(detail.get(key))
            if status:
                return status
    # 历史巡检记录只有 abnormal 布尔值，兼容读取。
    if "abnormal" in detail:
        abnormal = detail.get("abnormal")
        if isinstance(abnormal, bool):
            return FAILED if abnormal else PASSED
    return default


def normalize_detail(detail: dict[str, Any], default_status: str = "") -> dict[str, Any]:
    """把一条规则执行明细归一成统一结构，保留原始业务字段。"""
    normalized = dict(detail or {})
    status = _detail_status(normalized, default_status)
    if status:
        normalized["状态"] = status
    normalized["执行状态"] = normalized.get("状态", "")
    table = normalized.get("目标表名") or normalized.get("table_name") or ""
    if table:
        normalized.setdefault("目标表名", table)
        # 规范表名用于分组和去重，原始写法仍保留在“目标表名”里。
        normalized["规范表名"] = canonical_table(table)
    return normalized


def flatten_details(items: Iterable[Any] | None) -> list[dict[str, Any]]:
    """展开阶段小计，取其子明细，避免把小计本身当成规则或丢掉真实结果。

    阶段汇总行若携带 `明细 / 规则明细 / children`，只展开子明细；
    纯汇总行（没有子明细）直接忽略，不参与整轮统计。
    """
    flattened: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        if is_subtotal(raw):
            children = (
                raw.get("明细")
                or raw.get("规则明细")
                or raw.get("children")
                or raw.get("rules")
            )
            if children:
                flattened.extend(flatten_details(children))
            continue
        children = raw.get("明细") or raw.get("规则明细")
        if children and not any(
            raw.get(key) for key in ("规则编号", "rule_id", "规则名称")
        ):
            # 阶段容器（非小计）也只取子明细，容器自身没有规则身份。
            flattened.extend(flatten_details(children))
            continue
        flattened.append(raw)
    return flattened


def merge_rule_details(
    existing: Iterable[dict[str, Any]] | None,
    incoming: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """按规则编号合并明细，并发重复上报只保留一条。

    终态（通过/失败）优先于未执行/跳过；两条终态同时到达时以后到的为准，
    但已写入的确认字段（如确认人、确认时间）会保留，供超时补算复用。
    阶段小计只展开子明细，绝不把“小计”本身与整轮相加。
    """
    merged: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    counters = 0

    for raw in flatten_details(list(existing or []) + list(incoming or [])):
        counters += 1
        detail = normalize_detail(raw)
        key = rule_key(detail) or f"anonymous:{counters}"
        detail.setdefault("规则编号", detail.get("rule_id") or detail.get("id") or key)

        previous = merged.get(key)
        if previous is None:
            merged[key] = detail
            continue

        # 保留上一版已有的确认/审计字段，补算时不把人工确认覆盖掉。
        for field, value in previous.items():
            if field not in detail or detail[field] in (None, ""):
                detail[field] = value

        previous_status = previous.get("状态", "")
        current_status = detail.get("状态", "")
        if current_status in TERMINAL_STATUSES:
            merged[key] = detail
        elif previous_status in TERMINAL_STATUSES:
            # 未执行/跳过的重复上报不能覆盖已经完成的终态：
            # 保留原终态明细，不能只换状态而留下“结果未出”这类过期原因。
            merged[key] = previous
        else:
            merged[key] = detail

    return list(merged.values())


# 值班人员口头/配置里常用的表别名，统一映射到实际物理表名。
TABLE_ALIASES = {
    "订单表": "ods_order_info",
    "订单": "ods_order_info",
    "order": "ods_order_info",
    "orders": "ods_order_info",
    "ods_order_info": "ods_order_info",
    "广告表": "ads_sale_report",
    "广告": "ads_sale_report",
    "ads_sale_report": "ads_sale_report",
    "交易明细表": "dwd_trade_detail",
    "用户表": "ods_user_profile",
    "商品表": "dim_product_sku",
    "日志表": "dwd_log_event",
}


def canonical_table(name: Any) -> str:
    text = str(name or "").strip()
    return TABLE_ALIASES.get(text, TABLE_ALIASES.get(text.casefold(), text))


def split_scope(data_scope: Any) -> list[str]:
    """拆分数据范围，兼容逗号、中文逗号、分号、竖线和空白分隔。"""
    if data_scope is None:
        return []
    if isinstance(data_scope, (list, tuple, set)):
        parts = [str(item) for item in data_scope]
    else:
        text = str(data_scope)
        for separator in ("，", "、", ";", "；", "|", "\n", "\t", " "):
            text = text.replace(separator, ",")
        parts = text.split(",")
    result: list[str] = []
    for part in parts:
        value = part.strip()
        if value and value not in result:
            result.append(value)
    return result


def in_scope(table_name: Any, data_scope: Any) -> bool:
    """判断规则的目标表是否落在本次巡检的数据范围内。

    范围里写“订单表/广告表”这类别名时，也能和实际表名对上。
    """
    table = str(table_name or "").strip()
    if not table:
        return False
    scope_list = split_scope(data_scope)
    if not scope_list:
        return False

    table_canonical = canonical_table(table)
    scope_canonical = {canonical_table(item) for item in scope_list}
    if table in scope_list or table_canonical in scope_canonical:
        return True
    # 兼容历史调用方把范围写成一段自由文本的情况。
    raw_scope = str(data_scope)
    return table in raw_scope or table_canonical in raw_scope


def _rate(passed: int, executed: int, empty: str = "0.0%") -> str:
    if executed <= 0:
        return empty
    return f"{passed / executed * 100:.1f}%"


def _rate_value(passed: int, executed: int) -> float:
    if executed <= 0:
        return 0.0
    return round(passed / executed * 100, 1)


def compute_table_stats(details: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按目标表聚合明细，供日报复查面板判断每张表能否用本轮结果确认。"""
    grouped: dict[str, list[dict[str, Any]]] = OrderedDict()
    for detail in details:
        table = str(
            detail.get("规范表名")
            or canonical_table(detail.get("目标表名") or detail.get("table_name"))
            or ""
        ).strip()
        if not table:
            table = "（未标注表）"
        grouped.setdefault(table, []).append(detail)

    result: dict[str, dict[str, Any]] = {}
    for table, table_details in grouped.items():
        counts = {status: 0 for status in STATUS_ORDER}
        for detail in table_details:
            status = detail.get("状态", "")
            if status in counts:
                counts[status] += 1
        executed = counts[PASSED] + counts[FAILED]
        applicable = executed + counts[NOT_RUN]
        result[table] = {
            "目标表名": table,
            "适用规则数": applicable,
            "通过数": counts[PASSED],
            "失败数": counts[FAILED],
            "未执行数": counts[NOT_RUN],
            "跳过数": counts[SKIPPED],
            "已执行数": executed,
            "通过率": _rate(counts[PASSED], executed),
            "通过率数值": _rate_value(counts[PASSED], executed),
            # 有未执行规则时，本轮结果不足以确认这张表。
            "结果状态": "未出结果" if counts[NOT_RUN] else "已出结果",
            "可确认": counts[NOT_RUN] == 0,
            "复查状态": (
                "待复查"
                if counts[NOT_RUN] or counts[FAILED]
                else "无需复查"
            ),
        }
    return result


def compute_stats(details: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """计算一轮巡检的完整统计。

    注意：传入的应当是整轮明细（已经合并过去重和小计）。这里不会再去相加
    各阶段小计，调用方也不应该把 `阶段小计` 与 `整轮` 相加。
    """
    unique = merge_rule_details([], details)
    counts = {status: 0 for status in STATUS_ORDER}
    for detail in unique:
        status = detail.get("状态", "")
        if status in counts:
            counts[status] += 1
        else:
            # 无法识别的状态按未执行处理，避免静默丢失规则。
            counts[NOT_RUN] += 1
            detail["状态"] = NOT_RUN
            detail["执行状态"] = NOT_RUN

    passed = counts[PASSED]
    failed = counts[FAILED]
    skipped = counts[SKIPPED]
    not_run = counts[NOT_RUN]
    executed = passed + failed
    applicable = executed + not_run
    total = applicable + skipped

    table_stats = compute_table_stats(unique)
    return {
        # 巡检总数只统计本轮适用规则，范围外规则单独记跳过。
        "总规则数": applicable,
        "规则总数": total,
        "适用规则数": applicable,
        "范围外规则数": skipped,
        "通过数": passed,
        "失败数": failed,
        "执行失败数": failed,
        "跳过数": skipped,
        "未执行数": not_run,
        "待执行数": not_run,
        "已执行数": executed,
        "执行数": executed,
        "实际执行数": executed,
        "通过率": _rate(passed, executed),
        "通过率数值": _rate_value(passed, executed),
        "巡检通过率": _rate(passed, executed),
        "规则明细": unique,
        "明细": unique,
        "表统计": table_stats,
        "按表统计": table_stats,
        "待复查表": [
            table for table, item in table_stats.items()
            if item["复查状态"] == "待复查"
        ],
        "可确认表": [
            table for table, item in table_stats.items()
            if item["可确认"]
        ],
        "未出结果表": [
            table for table, item in table_stats.items()
            if item["结果状态"] == "未出结果"
        ],
    }


def stats_as_legacy_fields(stats: dict[str, Any]) -> dict[str, Any]:
    """把巡检统计摊平成可直接写入巡检任务行/质量报告行的字段。"""
    return {
        "总规则数": stats.get("总规则数", 0),
        "通过数": stats.get("通过数", 0),
        "失败数": stats.get("失败数", 0),
        "跳过数": stats.get("跳过数", 0),
        "未执行数": stats.get("未执行数", 0),
        "已执行数": stats.get("已执行数", 0),
        "通过率": stats.get("通过率", "0.0%"),
        "规则明细": stats.get("规则明细", []),
        "表统计": stats.get("表统计", {}),
        "待复查表": stats.get("待复查表", []),
    }
