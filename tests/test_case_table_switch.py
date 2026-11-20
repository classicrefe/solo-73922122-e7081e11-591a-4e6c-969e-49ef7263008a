"""病例表更换场景：锁定身份绑定、换表保留规则、过期锁定提交、求解释放。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("SPLIT_DB_PATH", str(db_file))
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.db.close()


# 表 A：C1 属于 P1；表 B：C1 改指 P9，C2 仍属于 P1
TABLE_A = "病例号,患者号,标签\nC1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
TABLE_B = "病例号,患者号,标签\nC1,P9,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
QUOTAS = {"g1_label0": 1, "g1_label1": 1, "g2_label0": 1, "g2_label1": 1}


def _upload(client, text):
    r = client.post("/api/upload/cases",
                    files={"file": ("cases.csv", text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _locks(client, locks, table_id=None):
    body = {"locks": locks}
    if table_id is not None:
        body["case_table_id"] = table_id
    return client.put("/api/locks", json=body)


def _state(client):
    return client.get("/api/state").json()


def test_lock_identity_binds_patient_and_does_not_follow_case_id(client):
    _upload(client, TABLE_A)
    r = _locks(client, [{"case_id": "C1", "patient_id": "P1", "group": 1}])
    assert r.status_code == 200, r.text
    assert _state(client)["input"]["locks"] == [
        {"case_id": "C1", "patient_id": "P1", "group": 1}
    ]

    # 换表后 C1 指向 P9：旧锁定必须被丢弃，而不是跟随病例号迁移
    res = _upload(client, TABLE_B)
    assert res["kept_lock_count"] == 0
    assert res["dropped_locks"] == ["C1"]
    state = _state(client)
    assert state["input"]["locks"] == []
    assert state["case_table_epoch"] == 2


def test_only_same_case_and_patient_locks_survive_reupload(client):
    _upload(client, TABLE_A)
    assert _locks(client, [
        {"case_id": "C1", "patient_id": "P1", "group": 1},
        {"case_id": "C2", "patient_id": "P1", "group": 2},
    ]).status_code == 200

    res = _upload(client, TABLE_B)
    # C1 改指 P9 -> 丢弃；C2 仍为 P1 -> 保留
    assert res["dropped_locks"] == ["C1"]
    assert res["kept_lock_count"] == 1
    assert _state(client)["input"]["locks"] == [
        {"case_id": "C2", "patient_id": "P1", "group": 2}
    ]


def test_lock_save_with_stale_patient_identity_is_rejected_whole(client):
    _upload(client, TABLE_A)
    assert _locks(client, [{"case_id": "C2", "patient_id": "P1", "group": 2}]).status_code == 200
    version_before = _state(client)["dataset_version"]
    locks_before = _state(client)["input"]["locks"]

    _upload(client, TABLE_B)
    version_after_upload = _state(client)["dataset_version"]

    # 旧页面提交：C1 带着旧患者 P1，但当前表中 C1 属于 P9
    r = _locks(client, [
        {"case_id": "C1", "patient_id": "P1", "group": 1},
        {"case_id": "C3", "patient_id": "P2", "group": 1},
    ])
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "STALE_CASE_TABLE"
    # 整次拒绝：不许“部分成功”，版本与现行锁定保持换表后的状态
    state = _state(client)
    assert state["dataset_version"] == version_after_upload
    # C2/P1 在换表时因病例号与患者号均未变而保留；C1 的旧锁定已丢弃
    assert state["input"]["locks"] == [
        {"case_id": "C2", "patient_id": "P1", "group": 2}
    ]
    assert state["dataset_version"] > version_before and locks_before


def test_lock_save_against_old_case_table_id_is_rejected_without_mutation(client):
    _upload(client, TABLE_A)
    old_table_id = _state(client)["case_table_id"]
    _upload(client, TABLE_B)
    v = _state(client)["dataset_version"]

    # 旧表指纹 + 在新表上并不冲突的条目：仍须整次拒绝
    r = _locks(client, [{"case_id": "C2", "patient_id": "P1", "group": 2}],
               table_id=old_table_id)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "STALE_CASE_TABLE"
    state = _state(client)
    assert state["dataset_version"] == v
    assert state["input"]["locks"] == []


def test_lock_save_after_quota_change_on_same_table_still_valid(client):
    _upload(client, TABLE_A)
    table_id = _state(client)["case_table_id"]
    client.put("/api/quotas", json=QUOTAS)  # 版本 +1，但病例表未变

    r = _locks(client, [{"case_id": "C1", "patient_id": "P1", "group": 1}],
               table_id=table_id)
    assert r.status_code == 200, r.text
    assert r.json()["lock_count"] == 1
    assert _state(client)["input"]["locks"] == [
        {"case_id": "C1", "patient_id": "P1", "group": 1}
    ]


def test_lock_save_without_patient_id_is_still_checked_against_current_table(client):
    _upload(client, TABLE_A)
    old_table_id = _state(client)["case_table_id"]
    _upload(client, TABLE_B)
    v = _state(client)["dataset_version"]
    # 旧式载荷（无 patient_id）但携带旧病例表指纹：拒绝
    r = _locks(client, [{"case_id": "C2", "group": 2}], table_id=old_table_id)
    assert r.status_code == 409
    assert _state(client)["dataset_version"] == v


def test_locks_do_not_leak_into_next_manifest_after_table_switch(client):
    _upload(client, TABLE_A)
    client.put("/api/quotas", json=QUOTAS)
    assert _locks(client, [{"case_id": "C1", "patient_id": "P1", "group": 1}]).status_code == 200

    token = client.post("/api/solve").json()["preview_token"]
    mid_a = client.post("/api/publish", json={"preview_token": token}).json()["manifest_id"]
    assert client.get(f"/api/manifests/{mid_a}/download").status_code == 200

    _upload(client, TABLE_B)  # 换表，旧清单失效
    assert client.get(f"/api/manifests/{mid_a}/download").status_code == 409
    assert _state(client)["input"]["locks"] == []  # C1 改指 P9，旧锁定被丢弃

    client.put("/api/quotas", json=QUOTAS)
    solved = client.post("/api/solve").json()
    assert solved["result"]["status"] == "sat"
    # 新预览里不存在任何锁定，C1/P9 没被旧患者 P1 的锁定带入训练集
    assert solved["input_snapshot"]["locks"] == []

    mid_b = client.post("/api/publish",
                        json={"preview_token": solved["preview_token"]}).json()["manifest_id"]
    detail = client.get(f"/api/manifests/{mid_b}").json()
    assert detail["input"]["locks"] == []
    assert detail["case_table_epoch"] == 2
    assert detail["case_table_id"] == _state(client)["case_table_id"]
    dl = client.get(f"/api/manifests/{mid_b}/download")
    assert dl.status_code == 200
    body = dl.text.lstrip("﻿")
    assert "C1,P9,1," in body
    assert body.count(",否") == 4  # 无任何锁定标记


def test_resolve_and_publish_use_new_table_with_surviving_locks(client):
    _upload(client, TABLE_A)
    assert _locks(client, [
        {"case_id": "C1", "patient_id": "P1", "group": 1},
        {"case_id": "C2", "patient_id": "P1", "group": 1},
    ]).status_code == 200

    # 新表：C1 改指 P9（旧锁定丢弃），C3 仍为 P2 且病例号/患者号均未变
    table_c = "病例号,患者号,标签\nC1,P9,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\nC5,P3,1\nC6,P3,0\n"
    _upload(client, table_c)
    assert _locks(client, [
        {"case_id": "C2", "patient_id": "P1", "group": 1},
        {"case_id": "C3", "patient_id": "P2", "group": 2},
    ]).status_code == 200
    client.put("/api/quotas", json={"g1_label0": 2, "g1_label1": 1,
                                    "g2_label0": 1, "g2_label1": 2})
    solved = client.post("/api/solve").json()
    assert solved["result"]["status"] == "sat", solved
    assignments = solved["result"]["assignments"]
    assert assignments["C2"] == 1 and assignments["C3"] == 2
    assert solved["input_snapshot"]["case_table_epoch"] == 2
    assert {c["case_id"] for c in solved["input_snapshot"]["cases"]} == {
        "C1", "C2", "C3", "C4", "C5", "C6"}

    mid = client.post("/api/publish",
                      json={"preview_token": solved["preview_token"]}).json()["manifest_id"]
    text = client.get(f"/api/manifests/{mid}/download").text
    assert "C2,P1,0,1" in text and "C3,P2,1,2" in text


def test_stale_lock_submit_preserves_published_manifest_and_version(client):
    _upload(client, TABLE_A)
    client.put("/api/quotas", json=QUOTAS)
    token = client.post("/api/solve").json()["preview_token"]
    mid = client.post("/api/publish", json={"preview_token": token}).json()["manifest_id"]
    old_table_id = _state(client)["case_table_id"]

    _upload(client, TABLE_B)
    state_after = _state(client)
    v, locks = state_after["dataset_version"], state_after["input"]["locks"]

    r = _locks(client, [{"case_id": "C2", "patient_id": "P1", "group": 2}],
               table_id=old_table_id)
    assert r.status_code == 409
    after = _state(client)
    assert after["dataset_version"] == v
    assert after["input"]["locks"] == locks
    # 已发布清单状态照旧：换表时失效，不因被拒绝的提交而再次变动
    m = client.get(f"/api/manifests/{mid}").json()
    assert m["expired"] is True
    assert client.get(f"/api/manifests/{mid}/download").status_code == 409


def test_same_content_reupload_keeps_locks(client):
    _upload(client, TABLE_A)
    assert _locks(client, [{"case_id": "C1", "patient_id": "P1", "group": 1}]).status_code == 200
    res = _upload(client, TABLE_A)  # 同一份病例表（内容相同）
    assert res["kept_lock_count"] == 1
    assert _state(client)["input"]["locks"] == [
        {"case_id": "C1", "patient_id": "P1", "group": 1}
    ]


def test_concurrent_lock_saves_do_not_lose_updates(client):
    """并发保存锁定：事务内读取-修改-写入，版本唯一递增且最后一次完整生效。"""
    _upload(client, TABLE_A)
    import concurrent.futures

    def save(i):
        cid = f"C{(i % 4) + 1}"
        pid = "P2" if cid in ("C3", "C4") else "P1"
        return _locks(client, [{"case_id": cid, "patient_id": pid, "group": 1}]).json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(save, range(12)))
    versions = sorted(r["dataset_version"] for r in results)
    assert versions == list(range(2, 14))
    # 每次提交都是完整锁定表：最后一次写入的锁定数必为 1 且属于当前病例表
    state = _state(client)
    assert len(state["input"]["locks"]) == 1
    lock = state["input"]["locks"][0]
    assert lock["patient_id"] == {"C1": "P1", "C2": "P1",
                                  "C3": "P2", "C4": "P2"}[lock["case_id"]]


def test_concurrent_table_switch_and_lock_save_never_mixes_identities(client):
    """换表与保存锁定交错：最终状态要么是新表且锁定全部匹配，要么版本一致。"""
    _upload(client, TABLE_A)
    import concurrent.futures

    def upload_b():
        return _upload(client, TABLE_B)

    def save_lock():
        return _locks(client, [{"case_id": "C1", "patient_id": "P1", "group": 1}])

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(upload_b), pool.submit(save_lock),
                pool.submit(upload_b), pool.submit(save_lock)]
        [f.result() for f in futs]

    state = _state(client)
    patients = {c["case_id"]: c["patient_id"] for c in state["input"]["cases"]}
    for lock in state["input"]["locks"]:
        assert patients[lock["case_id"]] == lock["patient_id"]
