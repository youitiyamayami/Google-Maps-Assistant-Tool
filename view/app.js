/* ================================
 * app.js
 * 目的:
 *   - 出発地(Departure) と 到着地(Arrive) の検索ボタン/Enter押下で
 *     /api/search を叩き、結果を #box ポップアップに一覧表示する。
 *   - 候補クリックで該当の入力欄へ反映し、ポップアップを閉じる。
 *
 * 仕様ポイント:
 *   - ユーザー提示イメージ（#box/#close 切り替え）に準拠。
 *   - 検索開始直後に #box を開き「検索中…」を表示 → 応答で結果に差し替え。
 *   - Departure/Arrive の両方に同じ挙動を適用。
 * ================================ */

/** 定数: APIエンドポイントのパス（同一オリジン前提） */
const API_SEARCH_URL = "/api/search";

/** ユーティリティ: 要素取得（存在しないときはnull） */
const $ = (sel, root = document) => root.querySelector(sel);

/** ユーティリティ: 要素作成の糖衣構文
 *  - props は property/attribute/dataset によしなに適用
 */
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

/** 初期化処理: DOMContentLoaded 後にイベントを結線 */
document.addEventListener("DOMContentLoaded", () => {
  // ポップアップ参照取得
  boxEl = $("#box");
  closeBtn = $("#close");
  listEl = $("#search-results");
  summaryEl = $("#search-summary");

  // 閉じるボタン（×）で #box を閉じる
  if (closeBtn) {
    closeBtn.addEventListener("click", closeBox);
  }

  // Esc キーで閉じる（アクセシビリティ向上）
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeBox();
  });

  // --- Departure: クリック/Enter で検索 ---
  wireSearchForSection({
    sectionId: "Departure",
    buttonSel: "#departure-search",
    formSel: "#departure-form",
    inputSel: "#departure-input",
    previewSel: "#departure-selected",
  });

  // --- Arrive: クリック/Enter で検索（今回追加） ---
  wireSearchForSection({
    sectionId: "Arrive",
    buttonSel: "#arrive-search",
    formSel: "#arrive-form",
    inputSel: "#arrive-input",
    previewSel: "#arrive-selected",
  });
});

/** wireSearchForSection:
 *  - 指定セクションのボタン/フォームsubmitに検索ハンドラを配線
 */
function wireSearchForSection({ sectionId, buttonSel, formSel, inputSel, previewSel }) {
  const section = document.getElementById(sectionId);
  if (!section) return;

  const btn = $(buttonSel, section);
  if (btn) {
    btn.addEventListener("click", (e) =>
      performSearchFor({ e, sectionId, inputSel, previewSel })
    );
  }

  const form = $(formSel, section);
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      performSearchFor({ e, sectionId, inputSel, previewSel });
    });
  }
}

/** openBox: #box を表示（display:block）して aria-hidden を更新 */
function openBox() {
  if (!boxEl) return;
  boxEl.style.display = "block";
  boxEl.setAttribute("aria-hidden", "false");
}

/** closeBox: #box を非表示（display:none）して aria-hidden を更新、内容をクリア */
function closeBox() {
  if (!boxEl) return;
  boxEl.style.display = "none";
  boxEl.setAttribute("aria-hidden", "true");
  if (listEl) listEl.replaceChildren();
  if (summaryEl) summaryEl.textContent = "";
}

/** showLoadingBox: 検索開始時の「検索中…」を描画して #box を開く */
function showLoadingBox(q) {
  if (!boxEl || !listEl || !summaryEl) return;
  summaryEl.textContent = `「${q}」で検索中…`;
  listEl.replaceChildren();
  listEl.appendChild(el("li", { className: "loading" }, "検索中…"));
  openBox();
}

/** normalizeQuery: 簡易正規化（全角英数→半角、空白の正規化） */
function normalizeQuery(s) {
  return (s || "")
    .replace(/[！-～]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0))
    .replace(/\s+/g, " ")
    .trim();
}

/** safeText: レスポンスの text() を安全に取得 */
async function safeText(resp) {
  try {
    return await resp.text();
  } catch {
    return "";
  }
}

/** renderResults:
 *  - 検索結果（payload.hits）を #box にカード風で描画。
 *  - onPick: 候補選択時のコールバック（入力欄へ反映→#box閉じ）。
 */
function renderResults(payload, onPick) {
  if (!boxEl || !listEl || !summaryEl) return;

  const { query, hits = [], stats = {} } = payload || {};
  const files = Number.isFinite(stats.dir_files_considered) ? stats.dir_files_considered : 0;
  const zips  = Number.isFinite(stats.zip_files_considered) ? stats.zip_files_considered : 0;
  summaryEl.textContent = `「${query}」で検索: 走査ファイル ${files} / ZIP ${zips} / ヒット ${hits.length}件`;

  listEl.replaceChildren();

  if (hits.length === 0) {
    listEl.appendChild(el("li", { className: "no-results" }, "該当なし"));
    openBox();
    return;
  }

  for (const item of hits) {
    const title = item.value || item.relpath || item.path || "(no title)";
    const meta = [
      item.relpath || item.path || "",
      item.source ? `source:${item.source}` : "",
      item.zip ? `zip:${item.zip}` : "",
      item.reason ? `reason:${item.reason}` : "",
    ]
      .filter(Boolean)
      .join(" / ");

    const li = el(
      "li",
      { className: "result-item", tabIndex: 0, role: "button", dataset: { relpath: item.relpath || "" } },
      el("div", { className: "title" }, title),
      el(
        "div",
        { className: "snippet" },
        (() => {
          const span = el("span");
          // snippet はテキストとして安全に描画
          span.innerText = item.snippet || "";
          return span;
        })()
      ),
      el("div", { className: "meta" }, meta)
    );

    // クリック or Enter/Space で選択
    li.addEventListener("click", () => onPick(item));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onPick(item);
      }
    });

    listEl.appendChild(li);
  }

  openBox(); // 念のため開く（初回やゼロ件時にも対応）
}

/** performSearchFor:
 *  - 指定セクション(sectionId)の入力/プレビューに対して検索フローを実行。
 *  - Departure/Arrive で共通に使用。
 */
async function performSearchFor({ e, sectionId, inputSel, previewSel }) {
  e?.preventDefault?.();

  const section = document.getElementById(sectionId);
  if (!section) return;

  const inputEl = $(inputSel, section) || $("input[type='text']", section) || $("input", section);
  const previewEl = $(previewSel) || null;

  const rawQuery = (inputEl?.value ?? "").trim();
  if (!rawQuery) {
    alert("検索キーワードを入力してください。");
    return;
  }

  const q = normalizeQuery(rawQuery);

  // 検索開始直後に「検索中…」の #box を開く
  showLoadingBox(q);

  // GET /api/search?q=... で検索（dir + filename/content 両方）
  const params = new URLSearchParams({
    q,
    mode: "dir",
    scope: "both",
    limit: String(50),
  });

  let resp;
  try {
    resp = await fetch(`${API_SEARCH_URL}?${params.toString()}`, { method: "GET" });
  } catch (err) {
    console.error(err);
    if (summaryEl) summaryEl.textContent = "検索APIへの接続に失敗しました。";
    if (listEl) {
      listEl.replaceChildren();
      listEl.appendChild(el("li", { className: "no-results" }, "サーバが起動しているか確認してください。"));
    }
    openBox();
    return;
  }

  if (!resp.ok) {
    const msg = await safeText(resp);
    if (summaryEl) summaryEl.textContent = `検索に失敗しました（${resp.status}）`;
    if (listEl) {
      listEl.replaceChildren();
      listEl.appendChild(el("li", { className: "no-results" }, msg || "エラーが発生しました。"));
    }
    openBox();
    return;
  }

  const data = await resp.json();
  const hits = Array.isArray(data?.hits) ? data.hits : [];
  const stats = data?.stats || {};
  const payload = { query: data?.query ?? q, hits, stats };

  // 検索結果描画。選択時は該当セクションの入力欄に反映して閉じる。
  renderResults(payload, (picked) => {
    const title = picked.value || picked.relpath || picked.path || "";
    if (inputEl) inputEl.value = title;
    if (previewEl) {
      const src =
        picked.source === "zip" && picked.zip
          ? ` (${picked.source}:${picked.zip})`
          : picked.source
          ? ` (${picked.source})`
          : "";
      previewEl.textContent = `選択: ${title}  /  ファイル: ${picked.relpath || picked.path || ""}${src}`;
    }
    closeBox();
  });
}
