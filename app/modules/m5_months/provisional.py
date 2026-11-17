"""换表旧段的峰平谷暂定归类。

裁决（月度关账口径）：
1. 旧表只有首末总读数，旧段「总用量」是实测值（差值 × 倍率），按核定总用量
   全额计入当月电费，不估算、不少计、不滞后关账。
2. 旧段没有峰平谷证据，禁止把任何峰平谷分量写成实测。需要分时电费时，按本模块
   的确定性规则生成「暂定」分量，并逐行打证据状态 P 与依据码；总表行始终是实测 M。
3. 暂定分量只做费率拆分：三者之和与旧段实测总用量逐位相等（最大余额法收口），
   不改变任何总用量。补到旧段实测峰平谷证据后，下一批次用实测分量重算并冲回暂定电费。

依据优先级（同供电点 = 契约里 replaced_by 指向的新表）：
- SUCCESSOR_TOU：新表同月峰平谷实测占比（同台区同月份的负荷曲线最接近旧段）；
- CONTRACT_WEIGHT：新表也没有可用实测时，用 config.DEFAULT_TOU_WEIGHTS。
旧表/新表契约都不含峰平谷寄存器时，不生成暂定分量，旧段按总表单一制电价出账。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from ..m2_contract import contract as contract_mod
from . import weights


def retired_meter_ids(contracts: dict[str, contract_mod.MeterContract]) -> set[str]:
    """契约里已被替换（有 replaced_at + replaced_by）的旧表号。"""
    return {c.meter_id for c in contracts.values() if c.replaced_at and c.replaced_by}


def successor_of(contracts: dict[str, contract_mod.MeterContract],
                 meter_id: str) -> contract_mod.MeterContract | None:
    old = contracts.get(meter_id)
    if old is None or not old.replaced_by:
        return None
    return contracts.get(old.replaced_by)


def needs_provisional_split(contracts: dict[str, contract_mod.MeterContract],
                            meter_id: str) -> bool:
    """旧表已换表，且同供电点新表契约含峰平谷——才需要暂定分时。"""
    successor = successor_of(contracts, meter_id)
    if successor is None:
        return False
    return all(name in successor.registers for name in config.TOU_REGISTERS)


def successor_tou_weights(measured_by_register: dict[str, Decimal]) -> dict[str, Decimal] | None:
    """用新表同月实测峰平谷算占比；全为零/缺失时返回 None 走兜底权重。"""
    values = {name: measured_by_register.get(name, Decimal("0"))
              for name in config.TOU_REGISTERS}
    total = sum(values.values(), Decimal("0"))
    if total <= 0:
        return None
    return {name: values[name] / total for name in config.TOU_REGISTERS}


def split_total(total: Decimal, weight_map: dict[str, Decimal]) -> dict[str, Decimal]:
    """按权重把实测总用量拆成暂定三分量，最大余额法收口到 0.001，合计逐位相等。"""
    slices = [(name, weight_map.get(name, Decimal("0")))
              for name in config.TOU_REGISTERS]
    parts = weights.largest_remainder(total, slices)
    return {name: value for name, value in parts}


def provision_for_bucket(*, month: str, meter_id: str, total_usage: Decimal,
                         contracts: dict[str, contract_mod.MeterContract],
                         measured_tou_by_month_meter: dict[tuple, dict[str, Decimal]],
                         ) -> dict | None:
    """为一个旧表月桶生成暂定归类；不需要拆分时返回 None。

    measured_tou_by_month_meter: (月份, 新表号) -> {寄存器: 实测用量}，
    由当月全部实测分摊汇总而来，保证依据来自同一批次的真实数据。
    """
    if total_usage <= 0 or not needs_provisional_split(contracts, meter_id):
        return None
    successor = successor_of(contracts, meter_id)
    basis = config.BASIS_CONTRACT_WEIGHT
    weight_map = dict(config.DEFAULT_TOU_WEIGHTS)
    successor_mix = successor_tou_weights(
        measured_tou_by_month_meter.get((month, successor.meter_id), {}))
    if successor_mix is not None:
        basis = config.BASIS_SUCCESSOR_TOU
        weight_map = successor_mix
    components = split_total(total_usage, weight_map)
    return {
        "month": month,
        "meter_id": meter_id,
        "successor_id": successor.meter_id,
        "basis": basis,
        "components": components,
    }
