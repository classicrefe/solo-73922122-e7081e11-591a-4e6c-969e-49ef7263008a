"""前端锁定草稿逻辑（app/static/lock_state.js）的行为测试。

用 Node 执行同一份脚本，保证页面刷新/换表时：
- 原患者在旧表上的未保存选择不会出现在同号新患者行；
- 已保存锁定只对“病例号 + 患者号”都相同的行回填；
- 提交载荷只含当前病例表的行，并携带病例表指纹。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "app" / "static" / "lock_state.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="需要 node 执行前端纯逻辑脚本")


def _run(js_expr):
    code = (
        f"const L=require({json.dumps(str(SCRIPT))});\n"
        f"console.log(JSON.stringify((()=>{{return {js_expr}}})()));\n"
    )
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_stale_draft_does_not_appear_on_reassigned_case():
    got = _run(
        "L.rowValues('new', {caseTableId:'old', choices:{C1:'1'}},"
        " [{case_id:'C1',patient_id:'P9'}], [])"
    )
    assert got == {"C1": ""}


def test_stale_saved_lock_does_not_appear_on_reassigned_case():
    got = _run(
        "L.rowValues('new', {caseTableId:'new', choices:{}},"
        " [{case_id:'C1',patient_id:'P9'}],"
        " [{case_id:'C1',patient_id:'P1',group:1}])"
    )
    assert got == {"C1": ""}


def test_same_table_draft_and_saved_values_round_trip():
    assert _run(
        "L.rowValues('t1', {caseTableId:'t1', choices:{C1:'2'}},"
        " [{case_id:'C1',patient_id:'P1'}],"
        " [{case_id:'C1',patient_id:'P1',group:1}])"
    ) == {"C1": "2"}
    assert _run(
        "L.rowValues('t1', {caseTableId:'t1', choices:{}},"
        " [{case_id:'C1',patient_id:'P1'}],"
        " [{case_id:'C1',patient_id:'P1',group:1}])"
    ) == {"C1": "1"}


def test_sync_draft_discards_choices_when_table_changes():
    # 换表：旧草稿整体作废，从空草稿重新开始
    assert _run(
        "L.syncDraft({caseTableId:'old', choices:{C1:'1'}}, 'new')"
    ) == {"caseTableId": "new", "choices": {}}
    # 同一病例表：草稿保留（不会冻结其他行，也不会压掉别处刚保存的锁定）
    assert _run(
        "L.syncDraft({caseTableId:'t1', choices:{C1:'1'}}, 't1')"
    ) == {"caseTableId": "t1", "choices": {"C1": "1"}}


def test_record_choice_ignores_rows_from_old_table():
    assert _run(
        "L.recordChoice({caseTableId:'old', choices:{C1:'1'}}, 'new', 'C2', '2')"
    ) == {"caseTableId": "new", "choices": {"C2": "2"}}
    assert _run(
        "L.recordChoice({caseTableId:'t1', choices:{}}, 't1', 'C2', '2')"
    ) == {"caseTableId": "t1", "choices": {"C2": "2"}}


def test_unrelated_saved_lock_is_not_shadowed_by_draft():
    # 草稿只有 C1，另一个窗口保存的 C2 锁定仍应显示
    assert _run(
        "L.rowValues('t1', {caseTableId:'t1', choices:{C1:'2'}},"
        " [{case_id:'C1',patient_id:'P1'},{case_id:'C2',patient_id:'P2'}],"
        " [{case_id:'C2',patient_id:'P2',group:2}])"
    ) == {"C1": "2", "C2": "2"}


def test_payload_only_contains_rows_rendered_from_current_table():
    got = _run(
        "L.collectPayload('new', ["
        "{case_id:'C1',patient_id:'P9',value:'1',table_id:'old'},"
        "{case_id:'C2',patient_id:'P9',value:'',table_id:'new'},"
        "{case_id:'C3',patient_id:'P9',value:'2',table_id:'new'}], [])"
    )
    assert got == {
        "locks": [{"case_id": "C3", "patient_id": "P9", "group": 2}],
        "case_table_id": "new",
    }


def test_filtered_save_keeps_locks_of_hidden_cases():
    # 筛选后只渲染了 C1；C2 的已保存锁定不能被“筛选后保存”误删
    got = _run(
        "L.collectPayload('t1',"
        " [{case_id:'C1',patient_id:'P1',value:'',table_id:'t1'}],"
        " [{case_id:'C1',patient_id:'P1',group:1},"
        "  {case_id:'C2',patient_id:'P2',group:2}])"
    )
    assert got == {
        "locks": [{"case_id": "C2", "patient_id": "P2", "group": 2}],
        "case_table_id": "t1",
    }


def test_hidden_case_draft_choice_wins_over_saved_value():
    got = _run(
        "L.collectPayload('t1', [],"
        " [{case_id:'C1',patient_id:'P1',group:1}],"
        " {caseTableId:'t1', choices:{C1:'2'}})"
    )
    assert got["locks"] == [{"case_id": "C1", "patient_id": "P1", "group": 2}]
    # 旧表草稿不得影响当前表
    got2 = _run(
        "L.collectPayload('t1', [],"
        " [{case_id:'C1',patient_id:'P1',group:1}],"
        " {caseTableId:'old', choices:{C1:''}})"
    )
    assert got2["locks"] == [{"case_id": "C1", "patient_id": "P1", "group": 1}]
