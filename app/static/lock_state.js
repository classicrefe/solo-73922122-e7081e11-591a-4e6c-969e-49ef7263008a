/* 锁定草稿与提交载荷的纯逻辑（无 DOM 依赖，便于测试）。
 *
 * 关键不变量：
 * - 草稿绑定到一份病例表（case_table_id）；换表即整体作废。
 * - 草稿只记录用户实际改动过的行（dirty rows），不冻结所有行的旧值，
 *   因此另一个窗口刚保存的锁定不会被本窗口的“空草稿”压住。
 * - 回填某一行时，只有“已保存锁定 + 同一患者”或“同一病例表上的草稿”才生效；
 *   原患者在旧表上的未保存选择绝不会出现在同号新患者那一行。
 * - 提交时只收集渲染自当前病例表的选择，并携带病例表指纹；筛选未显示的病例
 *   沿用已保存锁定（或同一表上的草稿），避免“筛选后保存”误删。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LockState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function newDraft(tableId) {
    return { caseTableId: tableId, choices: {} };
  }

  /* 同一病例表：保留草稿；换表：整体作废，从空草稿重新开始。 */
  function syncDraft(draft, tableId) {
    if (!draft || draft.caseTableId !== tableId) return newDraft(tableId);
    return draft;
  }

  /* 记录用户在某一行的未保存选择；不属于当前病例表的改动直接忽略。 */
  function recordChoice(draft, tableId, caseId, value) {
    const base = syncDraft(draft, tableId);
    base.choices[caseId] = String(value ?? "");
    return base;
  }

  function rowValues(tableId, draft, cases, savedLocks) {
    const savedByCase = {};
    for (const lock of savedLocks || []) {
      if (lock && lock.case_id) savedByCase[lock.case_id] = lock;
    }
    const draftOk = draft && draft.caseTableId === tableId;
    const out = {};
    for (const c of cases || []) {
      const saved = savedByCase[c.case_id];
      if (draftOk &&
          Object.prototype.hasOwnProperty.call(draft.choices, c.case_id)) {
        out[c.case_id] = draft.choices[c.case_id];
      } else if (saved && saved.patient_id === c.patient_id) {
        out[c.case_id] = String(saved.group);
      } else {
        // 已保存锁定指向的是同号的其他患者：视为未锁定
        out[c.case_id] = "";
      }
    }
    return out;
  }

  /* 收集整张锁定表。
   *
   * - 只提交渲染自当前病例表的选择；换表前的旧 DOM 绝不参与提交。
   * - 筛选只影响显示：未显示在当前 DOM 中的病例沿用已保存的锁定，
   *   否则“筛选后保存”会误删未显示病例的锁定。
   */
  function collectPayload(tableId, domRows, savedLocks, draft) {
    const locks = [];
    const seen = new Set();
    for (const row of domRows || []) {
      if (!row || !row.table_id || row.table_id !== tableId) continue;
      seen.add(row.case_id);
      if (row.value === "" || row.value === undefined || row.value === null) continue;
      locks.push({
        case_id: row.case_id,
        patient_id: row.patient_id,
        group: Number(row.value),
      });
    }
    const draftChoices = draft && draft.caseTableId === tableId ? draft.choices : {};
    for (const saved of savedLocks || []) {
      if (!saved || seen.has(saved.case_id)) continue;
      let value = saved.group;
      if (Object.prototype.hasOwnProperty.call(draftChoices, saved.case_id)) {
        // 未显示的行若被用户改过草稿，以草稿为准
        value = draftChoices[saved.case_id];
      }
      if (value === "" || value === undefined || value === null) continue;
      locks.push({
        case_id: saved.case_id,
        patient_id: saved.patient_id,
        group: Number(value),
      });
    }
    return { locks, case_table_id: tableId };
  }

  return { newDraft, syncDraft, recordChoice, rowValues, collectPayload };
});
