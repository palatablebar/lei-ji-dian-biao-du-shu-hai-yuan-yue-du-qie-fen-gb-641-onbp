"""换表暂定归类的端到端：临时样例 → HTTP 跑批 → 页面与下载都标清实测/暂定。

通过 monkeypatch 注册一个只在本测试生效的样例名，不碰生产白名单与演示数据。
"""
from __future__ import annotations

from app import config
from app.main import create_app
from app.store import Store
from fastapi.testclient import TestClient

METERS = """meter_id,name,capacity,multiplier,unit,registers,replaced_at,replaced_by
M0100,旧专线,99999.9,1,kWh,TOTAL,2026-02-10T00:00:00+08:00,M0101
M0101,新专线,99999.9,1,kWh,TOTAL;PEAK;FLAT;VALLEY,,
"""
PRICES = """register,price
TOTAL,0.65
PEAK,1.05
FLAT,0.65
VALLEY,0.32
"""
READINGS = """meter_id,register,read_at,reading,unit,read_type,note
M0100,TOTAL,2026-02-01T00:00:00+08:00,500.0,kWh,N,
M0100,TOTAL,2026-02-10T00:00:00+08:00,600.0,kWh,N,换表末读
M0101,TOTAL,2026-02-20T00:00:00+08:00,50.0,kWh,N,新表首读
M0101,PEAK,2026-02-20T00:00:00+08:00,25.0,kWh,N,
M0101,FLAT,2026-02-20T00:00:00+08:00,20.0,kWh,N,
M0101,VALLEY,2026-02-20T00:00:00+08:00,5.0,kWh,N,
"""


def _client(tmp_path, monkeypatch):
    (tmp_path / "meters.csv").write_text(METERS, encoding="utf-8")
    (tmp_path / "prices.csv").write_text(PRICES, encoding="utf-8")
    (tmp_path / "swap_e2e.csv").write_text(READINGS, encoding="utf-8")
    monkeypatch.setitem(config.SAMPLES, "swap_e2e.csv", "换表暂定端到端")
    settings = config.Settings(db_path=str(tmp_path / "db.sqlite"),
                               sample_dir=str(tmp_path))
    return TestClient(create_app(settings))


def test_pages_and_downloads_mark_measured_vs_provisional(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    run = client.post("/api/runs", json={"sample": "swap_e2e.csv"})
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["reconcile"]["all_balanced"] is True

    # 月度分摊页：暂定徽章 + 旧段总用量实测同时出现
    months_page = client.get("/months").text
    assert "暂定" in months_page
    assert "实测" in months_page
    assert "暂定归类说明" in months_page

    # 月度 CSV：证据状态列把旧表峰平谷标暂定、总表标实测
    monthly = client.get("/api/export/monthly.csv").text
    lines = monthly.splitlines()
    header = lines[0].lstrip("﻿").split(",")
    assert header[6] == "证据状态" and header[7] == "归类依据"
    old_rows = [line for line in lines if line.startswith("2026-02,M0100,")]
    statuses = {line.split(",")[2]: (line.split(",")[6], line.split(",")[7])
                for line in old_rows}
    assert statuses["总"] == ("实测", "")
    assert statuses["峰"][0] == "暂定"
    assert "新表同月实测峰平谷占比" in statuses["峰"][1]

    # 区间 CSV：实际总读数（首末示数）与读数证据列
    intervals = client.get("/api/export/intervals.csv").text
    assert "读数证据" in intervals.splitlines()[0]
    assert "实测" in intervals

    # 运行清单：暂定策略写入清单，可离线复算
    manifest = client.get("/api/export/manifest.json").json()
    assert "provisional_policy" in manifest["manifest"]
    provisional_account = next(a for a in manifest["manifest"]["accounts"]
                               if a["key"] == "provisional")
    assert provisional_account["balanced"] is True


def test_rerun_same_package_is_idempotent_over_http(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    first = client.post("/api/runs", json={"sample": "swap_e2e.csv"}).json()
    second = client.post("/api/runs", json={"sample": "swap_e2e.csv"}).json()
    assert second["batch_id"] == first["batch_id"]
    assert second["idempotent_replay"] is True
    assert second["counts"] == first["counts"]
    assert second["totals"]["interval_usage"] == first["totals"]["interval_usage"]
    assert second["totals"]["bill_amount"] == first["totals"]["bill_amount"]


def test_retry_done_batch_keeps_original_readings(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    bid = client.post("/api/runs", json={"sample": "swap_e2e.csv"}).json()["batch_id"]
    retry = client.post(f"/api/batches/{bid}/retry").json()
    assert retry["idempotent_replay"] is True
    assert retry["batch_id"] == bid
    # 重跑后读数证据 CSV 仍是同一批原始首末读数，没有被覆盖或追加
    readings = client.get("/api/export/readings.csv").text
    assert readings.count("M0100,TOTAL") == 2 or "M0100" in readings
    rows = [line for line in readings.splitlines()[1:] if line]
    assert len(rows) == 6
