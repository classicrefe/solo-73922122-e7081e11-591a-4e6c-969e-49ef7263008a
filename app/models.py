"""输入数据结构、CSV 解析与校验。"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .splitter import Case, RelativePair

MAX_CASE_ROWS = 200
MAX_RELATIVE_ROWS = 200

CASE_HEADER_ALIASES = {
    "病例号": "case_id",
    "case_id": "case_id",
    "caseid": "case_id",
    "id": "case_id",
    "患者号": "patient_id",
    "patient_id": "patient_id",
    "patientid": "patient_id",
    "标签": "label",
    "label": "label",
}

REL_HEADER_ALIASES = {
    "患者a": "patient_a",
    "患者_a": "patient_a",
    "patient_a": "patient_a",
    "患者b": "patient_b",
    "患者_b": "patient_b",
    "patient_b": "patient_b",
}


class ValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("；".join(errors))
        self.errors = errors


class StaleCaseTableError(Exception):
    """锁定提交所依据的病例表已被替换。

    与预览失效（StaleVersionError）区分开：配额等非病例表变动不会让锁定提交过期，
    只有病例表本身被替换才会。命中时整次提交拒绝，现行锁定与输入版本保持不变。
    """


@dataclass(frozen=True)
class CaseLock:
    """一条锁定绑定到“病例号 + 患者号”这一身份，而不只是病例号。

    病例号可能在新病例表中改指另一位患者；此时旧锁定绝不跟随病例号迁移。
    """

    case_id: str
    patient_id: str
    group: int

    def to_json(self) -> dict:
        return {
            "case_id": self.case_id,
            "patient_id": self.patient_id,
            "group": self.group,
        }


@dataclass
class ActiveInput:
    """当前生效的完整输入，整体作为一个版本持久化。"""

    cases: List[Case] = field(default_factory=list)
    relatives: List[RelativePair] = field(default_factory=list)
    # (group, label) -> 精确数量
    quotas: Dict[Tuple[int, int], int] = field(default_factory=dict)
    # case_id -> CaseLock（锁定同时绑定患者身份）
    locks: Dict[str, CaseLock] = field(default_factory=dict)
    # 病例表代次：仅病例表被替换时 +1；配额/亲属/锁定的变动不改变它。
    case_table_epoch: int = 0

    def lock_groups(self) -> Dict[str, int]:
        """求解器需要的 case_id -> group 视图。"""
        return {cid: lock.group for cid, lock in self.locks.items()}

    def case_table_id(self) -> str:
        """当前病例表的内容指纹（与配额/亲属/锁定无关）。"""
        return case_table_id(self.cases)

    def to_json(self) -> dict:
        return {
            "cases": [
                {"case_id": c.case_id, "patient_id": c.patient_id, "label": c.label}
                for c in self.cases
            ],
            "relatives": [
                {"patient_a": r.patient_a, "patient_b": r.patient_b}
                for r in self.relatives
            ],
            "quotas": {f"g{g}_label{l}": self.quotas.get((g, l), 0)
                       for g in (1, 2) for l in (0, 1)},
            "locks": [
                self.locks[cid].to_json() for cid in sorted(self.locks)
            ],
            "case_table_epoch": self.case_table_epoch,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ActiveInput":
        cases = [Case(**c) for c in data.get("cases", [])]
        patient_by_case = {c.case_id: c.patient_id for c in cases}
        locks: Dict[str, CaseLock] = {}
        # 兼容旧库：旧格式是 {case_id: group}，按当前病例表补全患者号；
        # 若病例号当前指向的患者与旧记录无法对应，则无法安全保留，直接丢弃。
        for item in _iter_lock_items(data.get("locks", {})):
            cid = item["case_id"]
            current_patient = patient_by_case.get(cid)
            if current_patient is None:
                continue
            patient = item.get("patient_id") or current_patient
            if patient != current_patient:
                continue
            locks[cid] = CaseLock(case_id=cid, patient_id=patient,
                                  group=int(item["group"]))
        return cls(
            cases=cases,
            relatives=[RelativePair(**r) for r in data.get("relatives", [])],
            quotas={
                (1, 0): data.get("quotas", {}).get("g1_label0", 0),
                (1, 1): data.get("quotas", {}).get("g1_label1", 0),
                (2, 0): data.get("quotas", {}).get("g2_label0", 0),
                (2, 1): data.get("quotas", {}).get("g2_label1", 0),
            },
            locks=locks,
            case_table_epoch=int(data.get("case_table_epoch", 0)),
        )


def _iter_lock_items(raw) -> List[dict]:
    """把新旧两种锁定存储格式统一成记录列表。"""
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and "case_id" in item and "group" in item:
                out.append(item)
        return out
    if isinstance(raw, dict):
        return [
            {"case_id": cid, "group": grp}
            for cid, grp in raw.items()
        ]
    return []


def case_table_id(cases: List[Case]) -> str:
    """病例表内容指纹：仅由病例号/患者号/标签决定。

    同一份病例表（内容完全相同）得到相同的 id；配额、亲属、锁定的变动都不影响它。
    因此可以用它核对“锁定提交所依据的病例表”，而不会因为单独改配额而误判过期。
    """
    digest = hashlib.sha256()
    for c in sorted(cases, key=lambda x: (x.case_id, x.patient_id, x.label)):
        digest.update(f"{c.case_id}\x1f{c.patient_id}\x1f{c.label}\x1e".encode("utf-8"))
    return digest.hexdigest()[:32]


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "")


def _read_csv_rows(raw: bytes) -> Tuple[List[str], List[List[str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValidationError([f"文件编码无法识别（需要 UTF-8 或 GB18030）：{exc}"])
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    if not rows:
        raise ValidationError(["CSV 文件为空。"])
    header = [_normalize_header(h) for h in rows[0]]
    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    return header, body


def parse_cases_csv(raw: bytes) -> List[Case]:
    header, body = _read_csv_rows(raw)
    errors: List[str] = []
    try:
        cols = [CASE_HEADER_ALIASES.get(h) for h in header]
    except KeyError:  # 兜底，正常不会走到
        cols = []
    missing = [
        name for name in ("case_id", "patient_id", "label") if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "病例 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_CASE_ROWS:
        raise ValidationError([f"病例数据最多 {MAX_CASE_ROWS} 行，当前 {len(body)} 行。"])
    if not body:
        raise ValidationError(["病例 CSV 没有数据行。"])

    idx = {name: cols.index(name) for name in ("case_id", "patient_id", "label")}
    cases: List[Case] = []
    seen: Dict[str, int] = {}
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        case_id = row[idx["case_id"]].strip()
        patient_id = row[idx["patient_id"]].strip()
        label_raw = row[idx["label"]].strip()
        if not case_id:
            errors.append(f"第 {line_no} 行：病例号为空")
            continue
        if not patient_id:
            errors.append(f"第 {line_no} 行：患者号为空（病例 {case_id}）")
            continue
        if label_raw not in ("0", "1"):
            errors.append(f"第 {line_no} 行：标签必须是 0 或 1，实际为 “{label_raw}”（病例 {case_id}）")
            continue
        if case_id in seen:
            errors.append(f"第 {line_no} 行：病例号 {case_id} 与第 {seen[case_id]} 行重复")
            continue
        seen[case_id] = line_no
        cases.append(Case(case_id=case_id, patient_id=patient_id, label=int(label_raw)))
    if errors:
        raise ValidationError(errors)
    return cases


def parse_relatives_csv(raw: bytes, known_patients: List[str]) -> List[RelativePair]:
    header, body = _read_csv_rows(raw)
    cols = [REL_HEADER_ALIASES.get(h) for h in header]
    missing = [
        name for name in ("patient_a", "patient_b") if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "亲属 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_RELATIVE_ROWS:
        raise ValidationError([f"亲属数据最多 {MAX_RELATIVE_ROWS} 行，当前 {len(body)} 行。"])
    if not body:
        raise ValidationError(["亲属 CSV 没有数据行。"])

    ia, ib = cols.index("patient_a"), cols.index("patient_b")
    known = set(known_patients)
    pairs: List[RelativePair] = []
    seen = set()
    errors: List[str] = []
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        a, b = row[ia].strip(), row[ib].strip()
        if not a or not b:
            errors.append(f"第 {line_no} 行：患者号不能为空")
            continue
        if a == b:
            errors.append(f"第 {line_no} 行：亲属对两端不能是同一患者（{a}）")
            continue
        for p in (a, b):
            if p not in known:
                errors.append(f"第 {line_no} 行：患者 {p} 不存在于病例数据中")
        key = tuple(sorted((a, b)))
        if key in seen:
            continue  # 完全重复的亲属对直接去重
        seen.add(key)
        pairs.append(RelativePair(patient_a=a, patient_b=b))
    if errors:
        raise ValidationError(errors)
    return pairs


def validate_quotas(payload: dict, total_by_label: Dict[int, int]) -> Dict[Tuple[int, int], int]:
    """仅做类型/范围校验；配额是否可行交给 z3 判定（无解时输出冲突集）。"""
    quotas: Dict[Tuple[int, int], int] = {}
    errors: List[str] = []
    for g in (1, 2):
        for lab in (0, 1):
            key = f"g{g}_label{lab}"
            val = payload.get(key)
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                errors.append(f"组{g} 标签{lab} 的配额必须是非负整数，收到：{val!r}")
                continue
            quotas[(g, lab)] = val
    if errors:
        raise ValidationError(errors)
    # 总数明显不可能时提前给出可读提示，但不阻断——仍由求解器给出 MUS
    return quotas


def validate_locks(
    payload: list, cases: List[Case]
) -> Dict[str, CaseLock]:
    """校验锁定并以 CaseLock（病例号 + 患者号 + 分组）形式返回。

    - 病例号必须存在于当前病例表；
    - 若请求带了 patient_id，必须与当前病例表中该病例号所指患者一致，
      防止旧页面的选择被套到“同号新患者”头上；
    - 分组必须是 1 或 2。
    """
    patient_by_case = {c.case_id: c.patient_id for c in cases}
    locks: Dict[str, CaseLock] = {}
    errors: List[str] = []
    stale: List[str] = []
    for item in payload:
        if not isinstance(item, dict) or "case_id" not in item or "group" not in item:
            errors.append(f"锁定项格式错误：{item!r}")
            continue
        cid = str(item["case_id"]).strip()
        grp = item["group"]
        current_patient = patient_by_case.get(cid)
        if current_patient is None:
            errors.append(f"锁定了不存在于当前病例表的病例：{cid}")
            continue
        claimed_patient = item.get("patient_id")
        if claimed_patient is not None and str(claimed_patient).strip() != current_patient:
            stale.append(
                f"病例 {cid} 在当前病例表中属于患者 {current_patient}，"
                f"提交所依据的却是患者 {claimed_patient}（病例表已更换）"
            )
            continue
        if isinstance(grp, bool) or grp not in (1, 2):
            errors.append(f"病例 {cid} 的锁定分组必须是 1 或 2，收到：{grp!r}")
            continue
        locks[cid] = CaseLock(case_id=cid, patient_id=current_patient, group=int(grp))
    if stale:
        raise StaleCaseTableError(
            "锁定提交所依据的病例表已被替换，整次提交已拒绝，现行锁定、输入版本与"
            "已发布清单均未改动，请刷新后重新核对：" + "；".join(stale)
        )
    if errors:
        raise ValidationError(errors)
    return locks
