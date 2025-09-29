/* ================================
 * app.js
 * 目的:
 *   - 出発地(Departure) と 到着地(Arrive) の検索ボタン/Enter押下で
 *     /api/stops を叩き、XML/GeoJSON由来の候補を #box ポップアップに一覧表示する。
 *   - 候補クリックで該当の入力欄へ反映し、ポップアップを閉じる。
 *   - 結果は「駅名orバス停名」「都道府県」「路線名」を見やすく表示。
 * ================================ */

/** 定数: APIエンドポイント */
const API_STOPS_URL = "/api/stops";   // 駅/バス停検索API

/** ユーティリティ: 要素取得（存在しないときはnull） */
const $ = (sel, root = document) => root.querySelector(sel);

/** ユーティリティ: 要素作成 */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === "dataset" && v && typeof v === "object") {
      Object.entries(v).forEach(([dk, dv]) => (node.dataset[dk] = dv));
    } else if (k in node) {
      node[k] = v;
    } else {
      node.setAttribute(k, v);
    }
  });
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

/** 参照保持（#box / #close / #search-results / #search-summary） */
let boxEl, closeBtn, listEl, summaryEl;

/** 初期化 */
document.addEventListener("DOMContentLoaded", () => {
  boxEl = $("#box");
  closeBtn = $("#close");
  listEl = $("#search-results");
  summaryEl = $("#search-summary");

  if (closeBtn) closeBtn.addEventListener("click", closeBox);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeBox(); });

  wireSearchForSection({
    sectionId: "Departure",
    buttonSel: "#departure-search",
    formSel: "#departure-form",
    inputSel: "#departure-input",
    previewSel: "#departure-selected",
  });

  wireSearchForSection({
    sectionId: "Arrive",
    buttonSel: "#arrive-search",
    formSel: "#arrive-form",
    inputSel: "#arrive-input",
    previewSel: "#arrive-selected",
  });
});

/** セクション単位で検索ハンドラを配線 */
function wireSearchForSection({ sectionId, buttonSel, formSel, inputSel, previewSel }) {
  const section = document.getElementById(sectionId);
  if (!section) return;

  const btn = $(buttonSel, section);
  if (btn) btn.addEventListener("click", (e) => performSearchFor({ e, sectionId, inputSel, previewSel }));

  const form = $(formSel, section);
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      performSearchFor({ e, sectionId, inputSel, previewSel });
    });
  }
}

/** #box の開閉/初期表示 */
function openBox(){ if (boxEl){ boxEl.style.display="block"; boxEl.setAttribute("aria-hidden","false"); } }
function closeBox(){
  if (!boxEl) return;
  boxEl.style.display="none"; boxEl.setAttribute("aria-hidden","true");
  if (listEl) listEl.replaceChildren();
  if (summaryEl) summaryEl.textContent = "";
}
function showLoadingBox(q){
  if (!boxEl || !listEl || !summaryEl) return;
  summaryEl.textContent = `「${q}」で検索中…`;
  listEl.replaceChildren();
  listEl.appendChild(el("li", { className: "loading" }, "検索中…"));
  openBox();
}

/** 簡易正規化 */
function normalizeQuery(s){
  return (s||"").replace(/[！-～]/g, ch => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0)).replace(/\s+/g, " ").trim();
}

/** APIからの text() 取得（失敗時は空） */
async function safeText(resp){ try{ return await resp.text(); } catch{ return ""; } }

/** 結果描画（駅名/バス停名・都道府県・路線名） */
function renderResults(payload, onPick){
  if (!boxEl || !listEl || !summaryEl) return;
  const { query, hits = [], stats = {} } = payload || {};
  const files = Number.isFinite(stats.dir_files_considered) ? stats.dir_files_considered : 0;
  const zips  = Number.isFinite(stats.zip_files_considered) ? stats.zip_files_considered : 0;
  const label = `駅/バス停 候補 ${hits.length}件 / 解析: dir ${files}, zip ${zips} / 所要 ${stats.elapsed_sec ?? "?"}s`;
  summaryEl.textContent = label;

  listEl.replaceChildren();
  if (hits.length === 0){
    listEl.appendChild(el("li", { className: "no-results" }, "該当なし"));
    openBox();
    return;
  }

  for (const item of hits){
    const title = item.name || item.value || "(no title)";
    const pref  = item.pref || "（都道府県 不明）";
    const line  = item.line || "（路線 不明）";
    const meta  = [
      item.source ? `source:${item.source}` : "",
      item.relpath ? item.relpath : "",
      item.zip ? `zip:${item.zip}` : "",
    ].filter(Boolean).join(" / ");

    const li = el(
      "li",
      { className: "result-item", tabIndex: 0, role: "button", dataset: { relpath: item.relpath || "" } },
      el("div", { className: "title" }, title),
      el("div", { className: "snippet" }, `${pref} / ${line}`),
      el("div", { className: "meta" }, meta)
    );

    li.addEventListener("click", () => onPick(item));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(item); }
    });

    listEl.appendChild(li);
  }
  openBox();
}

/** 検索フロー本体 */
async function performSearchFor({ e, sectionId, inputSel, previewSel }){
  e?.preventDefault?.();

  const section = document.getElementById(sectionId);
  if (!section) return;

  const inputEl = $(inputSel, section) || $("input[type='text']", section) || $("input", section);
  const previewEl = $(previewSel) || null;
  const rawQuery = (inputEl?.value ?? "").trim();

  if (!rawQuery){
    alert("検索キーワードを入力してください。");
    return;
  }
  const q = normalizeQuery(rawQuery);
  showLoadingBox(q);

  const params = new URLSearchParams({ q, limit: String(50) });
  let resp;
  try{
    resp = await fetch(`${API_STOPS_URL}?${params.toString()}`, { method: "GET" });
  }catch(err){
    console.error(err);
    if (summaryEl) summaryEl.textContent = "検索APIへの接続に失敗しました。";
    if (listEl){
      listEl.replaceChildren();
      listEl.appendChild(el("li", { className: "no-results" }, "サーバが起動しているか確認してください。"));
    }
    openBox();
    return;
  }

  if (!resp.ok){
    const msg = await safeText(resp);
    if (summaryEl) summaryEl.textContent = `検索に失敗しました（${resp.status}）`;
    if (listEl){
      listEl.replaceChildren();
      listEl.appendChild(el("li", { className: "no-results" }, msg || "エラーが発生しました。"));
    }
    openBox();
    return;
  }

  const data = await resp.json();
  const hits = Array.isArray(data?.hits) ? data.hits : [];
  const stats = data?.stats || {};
  renderResults({ query: data?.query ?? q, hits, stats }, (picked) => {
    const name = picked.name || picked.value || "";
    if (inputEl) inputEl.value = name;
    if (previewEl){
      const pref = picked.pref || "（都道府県 不明）";
      const line = picked.line || "（路線 不明）";
      previewEl.textContent = `選択: ${name}  /  ${pref} / ${line}`;
    }
    closeBox();
  });
}
