"""证据行：每个导出数字都能追回原始行号、原值、原因码与判定方法。"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m2_contract import contract as contract_mod

MONTHLY_HEADER = ["月份", "表号", "寄存器", "用量kWh", "金额元", "尾差分",
                  "证据状态", "归类依据", "调整原因"]
INTERVAL_HEADER = ["区间号", "表号", "寄存器", "起", "止", "天数", "上次读数",
                   "本次读数", "差值", "倍率", "单位", "用量kWh", "判定", "读数证据",
                   "标记"]
READING_HEADER = ["原始行号", "分段", "表号", "寄存器", "抄表时间UTC", "原始读数",
                  "单位", "折合读数", "类型", "状态", "原因码"]
QUARANTINE_HEADER = ["原始行号", "分段", "原因码", "原因", "等级", "字段", "原始值",
                     "表号", "说明"]


def monthly_rows(allocations: list[dict], bills: list[dict],
                 contracts: dict[str, contract_mod.MeterContract]) -> list[list[str]]:
    """每行一个 (月份, 表号, 寄存器)：金额只落在该寄存器自己的分项上，可按列求和。

    证据状态区分「实测」与「暂定」：换表旧段总表是实测，补出的峰平谷带暂定与依据，
    下载文件里不得把暂定归类写成实测电费。
    """
    bill_index = {(bill["month"], bill["meter_id"]): bill for bill in bills}
    amount_index: dict[tuple, str] = {}
    first_register: dict[tuple, str] = {}
    for bill in bills:
        key = (bill["month"], bill["meter_id"])
        if bill["components"]:
            first_register[key] = bill["components"][0]["register"]
        for item in bill["components"]:
            amount_index[(bill["month"], bill["meter_id"], item["register"])] = item["amount"]
    rows = []
    for item in allocations:
        key = (item["month"], item["meter_id"])
        bill = bill_index.get(key)
        amount = amount_index.get((item["month"], item["meter_id"], item["register"]), "")
        adjust = ""
        if bill is not None and bill["adjust_cents"] and \
                first_register.get(key) == item["register"]:
            adjust = str(bill["adjust_cents"])
        status = item.get("evidence_status", config.EVIDENCE_MEASURED)
        basis_code = item.get("basis_code")
        rows.append([
            item["month"], item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            f"{item['usage_kwh']:.3f}", amount, adjust,
            config.EVIDENCE_LABELS.get(status, status),
            config.BASIS_LABELS.get(basis_code, "") if basis_code else "",
            "K_TOU_ADJUSTED" if item["tou_adjusted"] else "",
        ])
    return rows


def interval_rows(intervals: list[dict]) -> list[list[str]]:
    rows = []
    for item in intervals:
        evidence = "估抄(不作锚点)" if "K_ESTIMATE_SKIPPED" in item["flags"] else "实测"
        rows.append([
            item["interval_id"], item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            item["start_at"], item["end_at"], f"{item['days']:.9f}",
            f"{item['prev_reading']:.3f}", f"{item['cur_reading']:.3f}",
            f"{item['delta']:.3f}", f"{item['multiplier']:.3f}", item["unit"],
            f"{item['usage_kwh']:.3f}",
            _method_label(item["method"]), evidence,
            " ".join(item["flags"]),
        ])
    return rows


def reading_rows(readings: list[dict]) -> list[list[str]]:
    rows = []
    for item in readings:
        rows.append([
            str(item["lineno"]), str(item["segment"]), item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            item["read_at"], item["reading_text"], item["unit_raw"] or "",
            "" if item["reading_norm"] is None else f"{Decimal(str(item['reading_norm'])):.3f}",
            item["read_type"], item["status"], item.get("code") or "",
        ])
    return rows


def quarantine_rows(quarantine: list[dict]) -> list[list[str]]:
    rows = []
    for item in quarantine:
        rows.append([
            str(item["lineno"]), str(item.get("segment") or 1), item["code"],
            reasons.label(item["code"]), item["level"], item.get("field") or "",
            item.get("raw_value") or "", item.get("meter_id") or "",
            item.get("detail") or "",
        ])
    return rows


def _method_label(method: str) -> str:
    return {"normal": "常规", "rollover": "满量翻转",
            "replacement_first": "新表首读", "replacement_last": "换表末读"}.get(
                method, method)
