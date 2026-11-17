"""换表月中切换：旧段实测总用量 + 暂定峰平谷归类的裁决测试。

场景（独立临时样例目录，不改动演示样例与生产白名单）：
- 旧表 M0100 只有 TOTAL 首末总读数，2026-02-10 换表，被 M0101 替换；
- 新表 M0101 是 TOTAL+峰平谷四寄存器表，换表后三个分量都有实测；
- 旧段总用量是实测（差值×倍率），峰平谷无证据 → 暂定归类，逐行标 P 与依据码。

测试直接编排 M1→M5 各模块（与批次服务同一条管线），绕过仅用于页面的样例白名单。
"""
from __future__ import annotations

from decimal import Decimal

from app import config
from app.modules.m1_ingest import decode, reader, sniff
from app.modules.m2_contract import contract as contract_mod
from app.modules.m2_contract import service as m2_service
from app.modules.m3_quality import service as m3_service
from app.modules.m4_intervals import restore as restore_mod
from app.modules.m5_months import allocate as allocate_mod
from app.modules.m5_months import provisional

METERS_TOU = """meter_id,name,capacity,multiplier,unit,registers,replaced_at,replaced_by
M0100,旧专线,99999.9,1,kWh,TOTAL,2026-02-10T00:00:00+08:00,M0101
M0101,新专线,99999.9,1,kWh,TOTAL;PEAK;FLAT;VALLEY,,
"""
METERS_TOTAL_ONLY = """meter_id,name,capacity,multiplier,unit,registers,replaced_at,replaced_by
M0200,旧总表,99999.9,1,kWh,TOTAL,2026-02-10T00:00:00+08:00,M0201
M0201,新总表,99999.9,1,kWh,TOTAL,,
"""
PRICES = """register,price
TOTAL,0.65
PEAK,1.05
FLAT,0.65
VALLEY,0.32
"""
READINGS_TOU = """meter_id,register,read_at,reading,unit,read_type,note
M0100,TOTAL,2026-02-01T00:00:00+08:00,500.0,kWh,N,
M0100,TOTAL,2026-02-10T00:00:00+08:00,600.0,kWh,N,换表末读
M0101,TOTAL,2026-02-20T00:00:00+08:00,50.0,kWh,N,新表首读
M0101,PEAK,2026-02-20T00:00:00+08:00,25.0,kWh,N,
M0101,FLAT,2026-02-20T00:00:00+08:00,20.0,kWh,N,
M0101,VALLEY,2026-02-20T00:00:00+08:00,5.0,kWh,N,
"""
READINGS_TOTAL_ONLY = """meter_id,register,read_at,reading,unit,read_type,note
M0200,TOTAL,2026-02-01T00:00:00+08:00,500.0,kWh,N,
M0200,TOTAL,2026-02-10T00:00:00+08:00,600.0,kWh,N,换表末读
M0201,TOTAL,2026-02-20T00:00:00+08:00,50.0,kWh,N,新表首读
"""


def _ingest_rows(payload: bytes) -> list[dict]:
    """与 m1_ingest.service.ingest 同一条解码/识别链路，绕开页面样例白名单。"""
    decoded = decode.decode_bytes(payload)
    records = reader.read_records(decoded["text"])
    header = records[0].fields
    header_map = sniff.build_header_map(header)
    rows = []
    for record in records[1:]:
        if sniff.is_repeated_header(record.fields, header) or sniff.is_blank_row(record.fields):
            continue
        rows.append({"lineno": record.lineno, "segment": 1,
                     "cells": {name: sniff.row_value(record.fields, header_map, name)
                               for name in header_map},
                     "raw": record.raw})
    return rows


def _build(tmp_path, *, meters: str, readings: str) -> dict:
    (tmp_path / "meters.csv").write_text(meters, encoding="utf-8")
    (tmp_path / "prices.csv").write_text(PRICES, encoding="utf-8")
    sample = tmp_path / "swap.csv"
    sample.write_text(readings, encoding="utf-8")
    settings = config.Settings(db_path=str(tmp_path / "db.sqlite"),
                               sample_dir=str(tmp_path))
    ingested_rows = _ingest_rows(sample.read_bytes())
    contracts = contract_mod.load(settings)
    records, _, _ = m2_service.interpret_rows(ingested_rows, contracts)
    quality = m3_service.evaluate(records, contracts)
    intervals = restore_mod.build_intervals(quality["valid"], quality["estimate"],
                                            contracts)
    prices = contract_mod.load_prices(settings)
    allocated = allocate_mod.allocate(intervals, contracts, prices)
    return {"settings": settings, "contracts": contracts, "intervals": intervals,
            **allocated}


def test_old_segment_total_is_measured_but_tou_is_provisional(tmp_path):
    out = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    old = {(a["register"], a["evidence_status"]): a
           for a in out["allocations"] if a["meter_id"] == "M0100"}

    # 旧段总表：实测 100 kWh（600 − 500），不被暂定污染
    assert old[("TOTAL", "M")]["usage_kwh"] == Decimal("100.000")
    assert old[("TOTAL", "M")]["basis_code"] is None

    # 旧表峰平谷不存在读数证据，三个分量全部为暂定，且合计逐位等于实测总用量
    provisional_sum = Decimal("0")
    for name in config.TOU_REGISTERS:
        row = old[(name, "P")]
        assert row["basis_code"] == config.BASIS_SUCCESSOR_TOU
        assert any("K_TOU_PROVISIONAL" in s for s in row["sources"])
        provisional_sum += row["usage_kwh"]
    assert provisional_sum == Decimal("100.000")


def test_provisional_split_uses_successor_measured_mix(tmp_path):
    out = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    old = {a["register"]: a for a in out["allocations"]
           if a["meter_id"] == "M0100" and a["evidence_status"] == "P"}
    # 新表实测占比 峰25/平20/谷5 = 0.5/0.4/0.1；暂定拆分按该占比，最大余额收口
    assert old["PEAK"]["usage_kwh"] == Decimal("50.000")
    assert old["FLAT"]["usage_kwh"] == Decimal("40.000")
    assert old["VALLEY"]["usage_kwh"] == Decimal("10.000")
    assert sum(a["usage_kwh"] for a in old.values()) == Decimal("100.000")


def test_new_meter_components_remain_measured(tmp_path):
    out = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    for a in out["allocations"]:
        if a["meter_id"] == "M0101":
            assert a["evidence_status"] == "M"
            assert a["basis_code"] is None


def test_total_only_swap_has_no_provisional_split(tmp_path):
    out = _build(tmp_path, meters=METERS_TOTAL_ONLY, readings=READINGS_TOTAL_ONLY)
    assert all(a["evidence_status"] == "M" for a in out["allocations"])
    assert out["stats"]["provisional_components"] == 0
    # 旧段按总表单一制电价出账：100 × 0.65
    bill = next(b for b in out["bills"] if b["meter_id"] == "M0200")
    assert bill["amount_yuan"] == Decimal("65.00")
    assert bill["evidence_status"] == "M"


def test_provisional_bill_marked_and_amount_recomputable(tmp_path):
    out = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    bill = next(b for b in out["bills"] if b["meter_id"] == "M0100")
    assert bill["evidence_status"] == "P"
    assert bill["basis_code"] == config.BASIS_SUCCESSOR_TOU
    assert bill["usage_kwh"] == Decimal("100.000")
    # 暂定电费 = 50×1.05 + 40×0.65 + 10×0.32 = 52.50 + 26.00 + 3.20
    assert bill["amount_yuan"] == Decimal("81.70")
    assert sum(Decimal(c["amount"]) for c in bill["components"]) == bill["amount_yuan"]
    assert all(c["evidence_status"] == "P" for c in bill["components"])


def test_provisional_split_does_not_change_measured_total(tmp_path):
    """守恒：暂定只做费率拆分，月度总用量与区间账仍只按主寄存器计一次。"""
    out = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    # 旧段 100 + 新表首读 50
    assert out["stats"]["usage_total"] == Decimal("150.000")
    # 暂定 3 个分量归在旧表 1 个月桶，暂定用量合计等于旧段实测总用量
    assert out["stats"]["provisional_components"] == 3
    assert out["stats"]["provisional_buckets"] == 1
    assert out["stats"]["provisional_usage"] == Decimal("100.000")
    primary = [a for a in out["allocations"] if a["register"] == "TOTAL"]
    assert sum(a["usage_kwh"] for a in primary) == Decimal("150.000")


def test_contract_weight_fallback_when_successor_has_no_measured_tou():
    contracts = contract_mod.parse_rows([
        {"meter_id": "A", "name": "旧", "capacity": "99999", "multiplier": "1",
         "unit": "kWh", "registers": "TOTAL",
         "replaced_at": "2026-02-10T00:00:00+08:00", "replaced_by": "B"},
        {"meter_id": "B", "name": "新", "capacity": "99999", "multiplier": "1",
         "unit": "kWh", "registers": "TOTAL;PEAK;FLAT;VALLEY",
         "replaced_at": "", "replaced_by": ""},
    ])
    plan = provisional.provision_for_bucket(
        month="2026-02", meter_id="A", total_usage=Decimal("100.000"),
        contracts=contracts, measured_tou_by_month_meter={})
    assert plan["basis"] == config.BASIS_CONTRACT_WEIGHT
    assert sum(plan["components"].values(), Decimal("0")) == Decimal("100.000")


def test_split_total_closes_to_exact_measured_total():
    parts = provisional.split_total(
        Decimal("100.001"),
        {"PEAK": Decimal("0.5"), "FLAT": Decimal("0.4"), "VALLEY": Decimal("0.1")})
    assert sum(parts.values(), Decimal("0")) == Decimal("100.001")
    assert all(v.quantize(Decimal("0.001")) == v for v in parts.values())


def test_allocate_is_deterministic_across_runs(tmp_path):
    first = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    second = _build(tmp_path, meters=METERS_TOU, readings=READINGS_TOU)
    signature = lambda out: [
        (a["month"], a["meter_id"], a["register"], str(a["usage_kwh"]),
         a["evidence_status"], a["basis_code"]) for a in out["allocations"]]
    assert signature(first) == signature(second)
