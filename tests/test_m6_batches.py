"""M6 批次与对账：run_id、输入摘要、幂等重跑、四账、失败重试。"""
from __future__ import annotations

from decimal import Decimal

from app.modules.m6_batches import compare, service
from fixtures import expected


def test_run_writes_manifest_with_digest(settings, store, batch_id):
    manifest = store.load_manifest(batch_id)
    assert len(manifest["input_digest"]) == 64
    assert manifest["run_id"].startswith("MB-")
    assert [item["name"] for item in manifest["inputs"]] == [
        "readings_dirty.csv", "meters.csv", "prices.csv"]
    assert manifest["rule_version"] == settings.rule_version
    assert manifest["all_balanced"] is True


def test_idempotent_rerun_returns_same_batch(settings, store, batch_id):
    again = service.run(settings, store, sample="readings_dirty.csv")
    assert again["idempotent_replay"] is True
    assert again["batch"]["batch_id"] == batch_id


def test_rerun_keeps_row_counts(settings, store, batch_id):
    before = store.count_rows(batch_id)
    service.run(settings, store, sample="readings_dirty.csv")
    assert store.count_rows(batch_id) == before
    assert before == {"meters": 8, "readings": 46, "intervals": 32,
                      "allocations": 40, "bills": 16, "quarantine": 14}


def test_four_accounts_balanced(run_result):
    reconcile = run_result["reconciliation"]
    assert reconcile["all_balanced"] is True
    assert reconcile["diff_count"] == 0
    keys = [item["key"] for item in reconcile["accounts"]]
    assert keys == ["readings", "intervals", "months", "bills", "provisional"]
    for item in reconcile["accounts"]:
        assert item["diff"] == 0
        assert item["balanced"] is True
    provisional = next(a for a in reconcile["accounts"] if a["key"] == "provisional")
    # 脏样例的换表对（M0004→M0005）两侧都只有总表，无峰平谷，不产生暂定分量
    assert provisional["left"] == provisional["right"] == Decimal("0.000")
    assert provisional["unit"] == "kWh"


def test_readings_account_identity(run_result):
    totals = run_result["totals"]
    assert totals["raw_lines"] == (totals["valid"] + totals["estimate"]
                                   + totals["quarantine"] + totals["blank"])
    assert (totals["valid"], totals["estimate"], totals["quarantine"],
            totals["blank"]) == (expected.VALID, expected.ESTIMATE,
                                 expected.QUARANTINE, expected.BLANK)


def test_batch_key_changes_with_sample(settings, store):
    dirty = service.run(settings, store, sample="readings_dirty.csv")
    clean = service.run(settings, store, sample="readings_clean.csv")
    assert dirty["batch"]["batch_id"] != clean["batch"]["batch_id"]
    assert dirty["batch"]["batch_key"] != clean["batch"]["batch_key"]


def test_retry_of_done_batch_replays_without_overwrite(settings, store, batch_id):
    """发布保护：已完成批次重试只回放，不覆盖原始读数、不新增用量或电费。"""
    before = store.count_rows(batch_id)
    totals_before = store.load_totals(batch_id)
    run_before = store.get_batch(batch_id)["run_id"]
    result = service.retry(settings, store, batch_id)
    assert result["status"] == "done"
    assert result["batch"]["batch_id"] == batch_id
    assert result["idempotent_replay"] is True
    assert store.count_rows(batch_id)["intervals"] == expected.INTERVALS
    assert store.count_rows(batch_id) == before
    assert store.load_totals(batch_id) == totals_before
    assert store.get_batch(batch_id)["run_id"] == run_before


def test_retry_unknown_batch_raises_not_found(settings, store):
    import pytest
    from app import errors
    with pytest.raises(errors.NotFound):
        service.retry(settings, store, "mb-000000000000")


def test_interval_and_month_accounts_match(run_result):
    totals = run_result["totals"]
    assert Decimal(totals["interval_usage"]) == Decimal(totals["month_usage"])
    assert Decimal(totals["interval_usage"]) == expected.TOTAL_USAGE


def test_replay_surfaces_batch_as_latest(settings, store):
    """重跑旧批次后，页面停在这次看的结果上，而不是更晚创建的别的批次。"""
    dirty = service.run(settings, store, sample="readings_dirty.csv")
    service.run(settings, store, sample="readings_clean.csv")
    assert store.latest_batch()["batch_id"] != dirty["batch"]["batch_id"]
    service.run(settings, store, sample="readings_dirty.csv")
    assert store.latest_batch()["batch_id"] == dirty["batch"]["batch_id"]


def test_compare_reports_unbalanced_when_diff(settings):
    reconcile = compare.build(
        raw_lines=10, valid=9, estimate=0, quarantine=1, blank=0,
        interval_usage=Decimal("100.000"), month_usage=Decimal("99.000"),
        tou_before=Decimal("0"), tou_after=Decimal("0"), tou_adjustments=0,
        bill_total=Decimal("65.00"), component_total=Decimal("65.00"),
        bill_count=1)
    assert reconcile["all_balanced"] is False
    assert reconcile["diff_count"] == 1
    diffed = [item for item in reconcile["accounts"] if not item["balanced"]]
    assert [item["key"] for item in diffed] == ["intervals"]
    assert diffed[0]["diff"] == 1


def test_failed_publish_keeps_previous_committed_version(settings, store, batch_id,
                                                         monkeypatch):
    """发布中断：落库阶段抛错时事务整体回滚，上一版读数/用量/电费原样保留。"""
    before_rows = store.count_rows(batch_id)
    before_totals = store.load_totals(batch_id)
    before_readings = store.load_readings(batch_id)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated disk failure during save_run")

    monkeypatch.setattr(store, "save_run", boom)
    import pytest
    with pytest.raises(RuntimeError, match="simulated disk failure"):
        # force 模式走到落库；失败必须回滚而不是留下半批
        service.run(settings, store, sample="readings_dirty.csv", mode="force")

    # 原始批次与全部已提交数据逐行不变，没有新增用量或电费
    assert store.get_batch(batch_id)["status"] == "done"
    assert store.count_rows(batch_id) == before_rows
    assert store.load_totals(batch_id) == before_totals
    assert store.load_readings(batch_id) == before_readings


def test_rerun_same_package_keeps_usage_and_bill_identical(settings, store, batch_id):
    """同一抄表包重跑：用量与电费逐位不变，不新增任何行。"""
    before_bills = store.load_bills(batch_id)
    before_intervals = store.load_intervals(batch_id)
    result = service.run(settings, store, sample="readings_dirty.csv")
    assert result["idempotent_replay"] is True
    assert result["batch"]["batch_id"] == batch_id
    assert store.load_bills(batch_id) == before_bills
    assert store.load_intervals(batch_id) == before_intervals
