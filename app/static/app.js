/* 病例切分平台前端（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let currentVersion = null;
let currentState = null;
let currentTableId = null;   // 当前病例表内容指纹
let lastPreview = null; // {token, version, result, input, caseTableId}

// 用户尚未保存的锁定选择与其所属病例表；换表后必须整体丢弃，
// 绝不能把原患者的选择带去同号新患者那一行。
let lockDraft = null; // {caseTableId, choices: {case_id: "1"|"2"|""}}

// --------------------------------------------------------------------------- //
// 基础请求
// --------------------------------------------------------------------------- //
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = data?.detail || `请求失败（HTTP ${resp.status}）`;
    const errs = data?.errors ? `：${data.errors.join("；")}` : "";
    throw new Error(msg + errs);
  }
  return data;
}

function setMsg(id, text, ok = false) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "msg " + (ok ? "ok" : "err");
}

function downloadCsv(filename, content) {
  const blob = new Blob(["﻿" + content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 标签页
// --------------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// --------------------------------------------------------------------------- //
// 模板下载
// --------------------------------------------------------------------------- //
$("tplCases").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("cases_template.csv", "病例号,患者号,标签\nC001,P001,1\nC002,P001,0\nC003,P002,1\n");
});
$("tplRel").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("relatives_template.csv", "患者A,患者B\nP001,P002\n");
});

// --------------------------------------------------------------------------- //
// 上传
// --------------------------------------------------------------------------- //
async function uploadCases() {
  const f = $("casesFile").files[0];
  if (!f) return setMsg("casesMsg", "请先选择病例 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("casesMsg", "上传中…");
  try {
    const r = await api("/api/upload/cases", { method: "POST", body: fd });
    // 换表即刻丢弃本地锁定草稿与旧预览，不给“旧患者选择流入新患者行”留窗口
    lockDraft = null;
    lastPreview = null;
    $("satBox").classList.add("hidden");
    $("unsatBox").classList.add("hidden");
    setMsg("casesMsg", `已上传 ${r.case_count} 个病例，新版本 v${r.dataset_version}。${r.notice}`, true);
    $("casesFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("casesMsg", err.message); }
}

async function uploadRelatives() {
  const f = $("relFile").files[0];
  if (!f) return setMsg("relMsg", "请先选择亲属关系 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("relMsg", "上传中…");
  try {
    const r = await api("/api/upload/relatives", { method: "POST", body: fd });
    setMsg("relMsg", `已上传 ${r.pair_count} 对亲属关系，新版本 v${r.dataset_version}`, true);
    $("relFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("relMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 渲染当前数据
// --------------------------------------------------------------------------- //
function renderState() {
  const version = currentState.dataset_version;
  const input = currentState.input;
  currentVersion = version;
  currentTableId = currentState.case_table_id;
  $("currentVersion").textContent = "v" + version;
  $("currentTable").textContent =
    `代次 ${currentState.case_table_epoch} · ${String(currentTableId).slice(0, 8)}…`;

  const totals = currentState.label_totals;
  $("totL0").textContent = totals[0];
  $("totL1").textContent = totals[1];
  $("t_l0").textContent = totals[0];
  $("t_l1").textContent = totals[1];

  $("overview").innerHTML = [
    ["病例数", input.cases.length],
    ["患者数", currentState.patient_count],
    ["亲属对数", input.relatives.length],
    ["锁定数", (input.locks || []).length],
    ["病例表代次", currentState.case_table_epoch],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  $("caseCount").textContent = input.cases.length;
  $("caseTable").querySelector("tbody").innerHTML = input.cases
    .map((c) => `<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.patient_id)}</td><td>${c.label}</td></tr>`)
    .join("");

  $("relCount").textContent = input.relatives.length;
  $("relTable").querySelector("tbody").innerHTML = input.relatives
    .map((r) => `<tr><td>${escapeHtml(r.patient_a)}</td><td>${escapeHtml(r.patient_b)}</td></tr>`)
    .join("");

  // 配额回填（仅当输入框未被聚焦时，避免打断输入）
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const el = $("q_" + key);
    if (document.activeElement !== el) el.value = input.quotas[key] ?? 0;
  }
  renderLocks();
  renderManifestTable();
}

// --------------------------------------------------------------------------- //
// 配额
// --------------------------------------------------------------------------- //
async function saveQuotas() {
  const payload = {};
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const v = Number($("q_" + key).value);
    if (!Number.isInteger(v) || v < 0) {
      return setMsg("quotaMsg", `配额 ${key} 必须是非负整数`);
    }
    payload[key] = v;
  }
  try {
    const r = await api("/api/quotas", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMsg("quotaMsg", `配额已保存，新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("quotaMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 锁定表
// --------------------------------------------------------------------------- //
function renderLocks() {
  if (!currentState) return;
  const input = currentState.input;
  const tableId = currentState.case_table_id;

  // 草稿只记录用户实际改动过的行；换表后整体作废。
  lockDraft = LockState.syncDraft(lockDraft, tableId);
  const values = LockState.rowValues(tableId, lockDraft, input.cases, input.locks);

  const filter = $("lockFilter").value.trim().toLowerCase();
  const visibleCases = input.cases.filter(
    (c) => !filter || c.case_id.toLowerCase().includes(filter) ||
      c.patient_id.toLowerCase().includes(filter));
  const rows = visibleCases
    .map((c) => {
      const effective = values[c.case_id] ?? "";
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td><select data-lock-case="${escapeHtml(c.case_id)}"
                    data-lock-patient="${escapeHtml(c.patient_id)}"
                    data-lock-table="${escapeHtml(tableId)}"
                    onchange="onLockChoice(this)">
          <option value=""${effective === "" ? " selected" : ""}>不锁定</option>
          <option value="1"${effective === "1" ? " selected" : ""}>训练集（组1）</option>
          <option value="2"${effective === "2" ? " selected" : ""}>评估集（组2）</option>
        </select></td></tr>`;
    }).join("");
  $("lockTable").querySelector("tbody").innerHTML = rows;
}

/* 记录未保存的选择。草稿绑定病例表，换表后旧选择不会迁移到同号新患者。 */
function onLockChoice(select) {
  if (!currentState) return;
  const tableId = currentState.case_table_id;
  if (select.dataset.lockTable !== tableId) return;  // 旧 DOM，忽略
  lockDraft = LockState.recordChoice(
    lockDraft, tableId, select.dataset.lockCase, select.value);
}

async function saveLocks() {
  if (!currentState) return;
  const tableId = currentState.case_table_id;
  const domRows = [];
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    domRows.push({
      case_id: sel.dataset.lockCase,
      patient_id: sel.dataset.lockPatient,
      value: sel.value,
      table_id: sel.dataset.lockTable,
    });
  });
  const payload = LockState.collectPayload(
    tableId, domRows, currentState.input.locks, lockDraft);
  try {
    const r = await api("/api/locks", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      // 携带保存所依据的病例表指纹，服务端据此拒绝过期表的提交
      body: JSON.stringify(payload),
    });
    lockDraft = LockState.newDraft(r.case_table_id);
    setMsg("lockMsg", `锁定已保存（${r.lock_count} 项），新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) {
    setMsg("lockMsg", err.message);
    if (/病例表/.test(err.message)) {
      // 换表了：丢弃草稿并立即拉取新表
      lockDraft = null;
      await refreshAll();
    }
  }
}

// --------------------------------------------------------------------------- //
// 求解
// --------------------------------------------------------------------------- //
async function runSolve() {
  $("solveStatus").textContent = "z3 求解中…";
  $("solveStatus").className = "msg";
  $("satBox").classList.add("hidden");
  $("unsatBox").classList.add("hidden");
  try {
    const r = await api("/api/solve", { method: "POST" });
    lastPreview = {
      token: r.preview_token,
      version: r.dataset_version,
      caseTableId: r.case_table_id,
      result: r.result,
      input: r.input_snapshot,
    };
    renderSolveResult();
    $("solveStatus").textContent =
      r.result.status === "sat" ? `求解成功（依据 v${r.dataset_version}）` : "约束无解";
    $("solveStatus").className = "msg " + (r.result.status === "sat" ? "ok" : "err");
  } catch (err) {
    $("solveStatus").textContent = err.message;
    $("solveStatus").className = "msg err";
  }
}

function previewIsStale() {
  return !lastPreview || lastPreview.version !== currentVersion;
}

function renderSolveResult() {
  if (!lastPreview) return;
  const { result } = lastPreview;
  if (result.status === "sat") {
    $("satBox").classList.remove("hidden");
    $("unsatBox").classList.add("hidden");
    const counts = result.stats.counts;
    $("solveStats").innerHTML = [
      ["连通分量数", result.stats.component_count],
      ["约束总数", result.constraint_count],
      ["训练集 标签0/1", `${counts.g1_label0} / ${counts.g1_label1}`],
      ["评估集 标签0/1", `${counts.g2_label0} / ${counts.g2_label1}`],
    ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");
    const stale = previewIsStale();
    $("previewWarn").classList.toggle("hidden", !stale);
    $("publishBtn").disabled = stale;
    renderAssignments();
  } else {
    $("satBox").classList.add("hidden");
    $("unsatBox").classList.remove("hidden");
    $("unsatSummary").textContent = result.summary;
    $("conflictTable").querySelector("tbody").innerHTML = result.conflicts
      .map((c, i) => `<tr>
        <td>${i + 1}</td>
        <td><span class="pill ${c.kind === "quota" ? "dead" : "warn"}">${escapeHtml(c.kind_name)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td><code>${escapeHtml(JSON.stringify(c.detail))}</code></td>
      </tr>`).join("");
  }
}

function renderAssignments() {
  if (!lastPreview || lastPreview.result.status !== "sat") return;
  const grpFilter = $("filterGroup").value;
  const labFilter = $("filterLabel").value;
  const locks = lastPreview.input.locks || [];
  const lockByCase = {};
  for (const item of locks) lockByCase[item.case_id] = item;
  const assigns = lastPreview.result.assignments;
  const rows = lastPreview.input.cases
    .filter((c) => (!grpFilter || String(assigns[c.case_id]) === grpFilter) &&
      (!labFilter || String(c.label) === labFilter))
    .map((c) => {
      const g = assigns[c.case_id];
      const lock = lockByCase[c.case_id];
      const lockText = lock && lock.patient_id === c.patient_id
        ? "是 → " + (lock.group === 1 ? "训练集" : "评估集")
        : "否";
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td>${g}</td>
        <td>${g === 1 ? "训练集" : "评估集"}</td>
        <td>${lockText}</td>
      </tr>`;
    }).join("");
  $("assignTable").querySelector("tbody").innerHTML = rows;
}

// --------------------------------------------------------------------------- //
// 发布
// --------------------------------------------------------------------------- //
async function publishManifest() {
  if (!lastPreview || !lastPreview.token) return;
  setMsg("publishMsg", "发布中…");
  try {
    await api("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_token: lastPreview.token }),
    });
    setMsg("publishMsg", "清单已发布并与输入依据一起持久化", true);
    await refreshAll();
  } catch (err) {
    setMsg("publishMsg", err.message);
    if (String(err.message).includes("失效")) {
      $("publishBtn").disabled = true;
      $("previewWarn").classList.remove("hidden");
    }
  }
}

// --------------------------------------------------------------------------- //
// 已发布清单
// --------------------------------------------------------------------------- //
function renderManifestTable() {
  if (!currentState) return;
  const tbody = $("manifestTable").querySelector("tbody");
  if (!currentState.manifests.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">暂无已发布清单</td></tr>`;
    return;
  }
  tbody.innerHTML = currentState.manifests.map((m) => {
    const status = m.expired
      ? `<span class="pill dead">已永久失效${m.invalidated ? "（输入已变动）" : "（版本不匹配）"}</span>`
      : `<span class="pill ok">有效（v${m.dataset_version}）</span>`;
    return `<tr>
      <td><code>${escapeHtml(m.manifest_id.slice(0, 8))}…</code></td>
      <td>v${m.dataset_version}</td>
      <td>${escapeHtml(m.created_at)}</td>
      <td>${m.case_count ?? ""}</td>
      <td>${status}</td>
      <td>
        <button onclick="showManifest('${m.manifest_id}')">查看依据</button>
        <button ${m.expired ? "disabled" : ""} onclick="downloadManifest('${m.manifest_id}')">下载 CSV</button>
      </td></tr>`;
  }).join("");
}

async function showManifest(id) {
  try {
    const m = await api(`/api/manifests/${id}`);
    $("manifestDetail").classList.remove("hidden");
    $("manifestJson").textContent = JSON.stringify(
      { manifest_id: m.manifest_id, dataset_version: m.dataset_version,
        created_at: m.created_at, expired: m.expired,
        input_basis: m.input, split_result: m.result }, null, 2);
  } catch (err) { alert(err.message); }
}

async function downloadManifest(id) {
  const resp = await fetch(`/api/manifests/${id}/download`);
  if (!resp.ok) {
    let msg = `下载被拒绝（HTTP ${resp.status}）`;
    try { msg = (await resp.json()).detail || msg; } catch { /* ignore */ }
    alert(msg);
    await refreshAll();
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `split_manifest_${id.slice(0, 8)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 版本日志
// --------------------------------------------------------------------------- //
async function loadVersionLog() {
  try {
    const r = await api("/api/version-log");
    $("logTable").querySelector("tbody").innerHTML = r.log.map((e) => `<tr>
      <td>v${e.dataset_version}</td>
      <td>${escapeHtml(e.change_type)}</td>
      <td><code>${escapeHtml(JSON.stringify(e.detail))}</code></td>
      <td>${escapeHtml(e.created_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="hint">暂无变动记录</td></tr>`;
  } catch (err) { /* 轮询场景静默 */ }
}

// --------------------------------------------------------------------------- //
// 全局刷新与后台轮询（支持两个窗口交错操作）
// --------------------------------------------------------------------------- //
async function refreshAll(showStaleBanner = false) {
  try {
    const prevVersion = currentVersion;
    const prevTableId = currentTableId;
    currentState = await api("/api/state");
    renderState();
    $("healthBadge").textContent = "已连接";
    $("healthBadge").className = "badge";
    const tableChanged = prevTableId !== null &&
      prevTableId !== currentState.case_table_id;
    const changed = prevVersion !== null && prevVersion !== currentState.dataset_version;
    if (tableChanged) {
      // 换表：旧预览、锁定草稿全部作废，原患者未保存的选择不得出现在新患者行
      lockDraft = null;
      lastPreview = null;
      $("satBox").classList.add("hidden");
      $("unsatBox").classList.add("hidden");
      $("solveStatus").textContent = "病例表已更换，请重新核对锁定并重新求解。";
      $("solveStatus").className = "msg err";
      setMsg("lockMsg", "病例表已更换：仅病例号与患者号均相同的锁定被保留，请重新核对。");
    }
    if (changed || showStaleBanner) {
      $("staleBanner").classList.toggle("hidden", !changed && !showStaleBanner);
      if (lastPreview && lastPreview.version !== currentVersion) {
        $("previewWarn").classList.remove("hidden");
        $("publishBtn").disabled = true;
      }
    }
    if (lastPreview) renderSolveResult();
  } catch (err) {
    $("healthBadge").textContent = "连接中断";
    $("healthBadge").className = "badge down";
  }
}

setInterval(refreshAll, 4000);
setInterval(() => {
  if ($("tab-log").classList.contains("active")) loadVersionLog();
}, 5000);

refreshAll();
