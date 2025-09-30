/* ==================================================
 * app.js（座標優先URL対応：N05(駅)/P11(バス停)）
 * 目的:
 *  - 出発/中間/到着の候補選択時にサーバから得た lon/lat を保持（data-*）
 *  - URL 組み立て時に「座標があれば lat,lng を優先」して Google マップを開く
 *  - 保存APIにも座標を同送し、/output へ座標優先URLを出力
 * ================================================== */

/** API エンドポイント定数 */
const API_STOPS_URL = "/api/stops";                 // 候補検索
const API_ROUTE_SAVE = "/api/route/save";           // 出発→到着の保存
const API_ROUTE_SAVE_LEG1 = "/api/route/save_leg1"; // 出発→中間の保存

/** DOM ユーティリティ */
const $ = (sel, root = document) => root.querySelector(sel);

/** ポップアップ参照 */
let boxEl, closeBtn, listEl, summaryEl;

/** 初期化 */
document.addEventListener("DOMContentLoaded", () => {
  // ポップアップ参照
  boxEl = $("#box");
  closeBtn = $("#close");
  listEl = $("#search-results");
  summaryEl = $("#search-summary");
  if (closeBtn) closeBtn.addEventListener("click", closeBox);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeBox(); });

  // 検索配線（候補選択で data-lat/data-lon を入力欄に保持）
  wireSearchForSection({
    sectionId: "Departure",
    buttonSel: "#departure-search",
    formSel: "#departure-form",
    inputSel: "#departure-input",
    previewSel: "#departure-selected",
  });
  wireSearchForSection({
    sectionId: "Waypoint",
    buttonSel: "#waypoint-search",
    formSel: "#waypoint-form",
    inputSel: "#waypoint-input",
    previewSel: "#waypoint-selected",
    submitTo: "#do-search-leg1",
  });
  wireSearchForSection({
    sectionId: "Arrive",
    buttonSel: "#arrive-search",
    formSel: "#arrive-form",
    inputSel: "#arrive-input",
    previewSel: "#arrive-selected",
  });

  // 入力変更でUI・プレビュー更新（手入力時は座標をクリア）
  ["#departure-input", "#waypoint-input", "#arrive-input", "#depart-date", "#depart-time"].forEach(sel => {
    const node = $(sel);
    if (!node) return;
    node.addEventListener("input", () => {
      if (node.type === "text") { node.dataset.lat = ""; node.dataset.lon = ""; node.dataset.source = ""; }
      refreshUI();
    });
  });

  // 実行: Googleマップを新規タブで開く（座標優先）
  $("#do-search")?.addEventListener("click", () => {
    const url = buildGmapsUrlFromForm(); // 出発→到着
    if (!url) { alert("出発地と到着地を入力してください。"); return; }
    window.open(url, "_blank", "noopener,noreferrer");
  });
  $("#do-search-leg1")?.addEventListener("click", () => {
    const url = buildGmapsUrlLeg1FromForm(); // 出発→中間
    if (!url) { alert("出発地と中間地点を入力してください。"); return; }
    window.open(url, "_blank", "noopener,noreferrer");
  });

  // 保存: /output へ書き出し（座標も同送）
  $("#save-url")?.addEventListener("click", () => onSaveUrlCommon("full"));
  $("#save-url-leg1")?.addEventListener("click", () => onSaveUrlCommon("leg1"));

  // コピー（プレビューのURLをコピー）
  $("#copy-url")?.addEventListener("click", async () => copyText($("#url-preview")?.value || ""));
  $("#copy-url-leg1")?.addEventListener("click", async () => copyText($("#url-preview-leg1")?.value || ""));

  // Enterで実行
  $("#departure-form")?.addEventListener("submit", (e)=>onSubmitSearch(e, "#do-search"));
  $("#waypoint-form")?.addEventListener("submit", (e)=>onSubmitSearch(e, "#do-search-leg1"));
  $("#arrive-form")?.addEventListener("submit", (e)=>onSubmitSearch(e, "#do-search"));

  refreshUI();
});

/** Enter submit を指定ボタンに転送 */
function onSubmitSearch(e, targetBtnSel){
  e?.preventDefault?.();
  const btn = $(targetBtnSel);
  if (!btn?.disabled) btn.click();
}

/** 軽い正規化（全角→半角、余分な空白除去） */
function normalizeQuery(s){
  return (s||"").replace(/[！-～]/g, ch => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0))
                 .replace(/\s+/g, " ")
                 .trim();
}

/** ローカル日付+時刻 → UNIX秒（departure_time 用） */
function toUnixEpochLocal(dateStr, timeStr){
  if (!dateStr || !timeStr) return null;
  try{
    const [y,m,d] = dateStr.split("-").map(Number);
    const [hh,mm] = time.split(":").map(Number);
  }catch{ /* fallback below */ }
  try{
    const [y,m,d] = dateStr.split("-").map(Number);
    const [hh,mm] = timeStr.split(":").map(Number);
    const dt = new Date(y, (m-1), d, hh, mm, 0, 0);
    return Math.floor(dt.getTime()/1000);
  }catch{ return null; }
}

/** 現在のフォーム値 + 各入力の data-lat/data-lon を含めて返す */
function getFormValues(){
  const dep = $("#departure-input");
  const wap = $("#waypoint-input");
  const arr = $("#arrive-input");

  const origin = (dep?.value || "").trim();
  const waypoint = (wap?.value || "").trim();
  const destination = (arr?.value || "").trim();

  const date = ($("#depart-date")?.value || "").trim();
  const time = ($("#depart-time")?.value || "").trim();

  // dataset から座標を取得（文字列→数値 or null）
  const depLat = dep?.dataset.lat ? Number(dep.dataset.lat) : null;
  const depLon = dep?.dataset.lon ? Number(dep.dataset.lon) : null;
  const wapLat = wap?.dataset.lat ? Number(wap.dataset.lat) : null;
  const wapLon = wap?.dataset.lon ? Number(wap.dataset.lon) : null;
  const arrLat = arr?.dataset.lat ? Number(arr.dataset.lat) : null;
  const arrLon = arr?.dataset.lon ? Number(arr.dataset.lon) : null;

  return {
    origin, waypoint, destination,
    depart_date: date, depart_time: time,
    origin_lat: Number.isFinite(depLat) ? depLat : null,
    origin_lon: Number.isFinite(depLon) ? depLon : null,
    waypoint_lat: Number.isFinite(wapLat) ? wapLat : null,
    waypoint_lon: Number.isFinite(wapLon) ? wapLon : null,
    destination_lat: Number.isFinite(arrLat) ? arrLat : null,
    destination_lon: Number.isFinite(arrLon) ? arrLon : null,
  };
}

/** 共通: Google マップ URL 構築（座標があれば lat,lng を優先） */
function buildGmapsUrlParam(originText, destText, depart_date, depart_time, originLat, originLon, destLat, destLon){
  if (!(originText || (Number.isFinite(originLat) && Number.isFinite(originLon)))) return null;
  if (!(destText   || (Number.isFinite(destLat)   && Number.isFinite(destLon))))   return null;

  const params = new URLSearchParams();
  params.set("api","1");
  // 座標優先: GeoJSON は [lon,lat] だが Google URL は "lat,lng" の順
  if (Number.isFinite(originLat) && Number.isFinite(originLon)){
    params.set("origin", `${originLat},${originLon}`);
  }else{
    params.set("origin", originText);
  }
  if (Number.isFinite(destLat) && Number.isFinite(destLon)){
    params.set("destination", `${destLat},${destLon}`);
  }else{
    params.set("destination", destText);
  }
  params.set("travelmode","transit");
  params.set("hl","ja"); params.set("gl","JP");

  const epoch = toUnixEpochLocal(depart_date, depart_time);
  params.set("departure_time", epoch ? String(epoch) : "now");
  return `https://www.google.com/maps/dir/?${params.toString()}`;
}

/** 出発→到着 */
function buildGmapsUrlFromForm(){
  const v = getFormValues();
  return buildGmapsUrlParam(
    v.origin, v.destination, v.depart_date, v.depart_time,
    v.origin_lat, v.origin_lon, v.destination_lat, v.destination_lon
  );
}

/** 出発→中間（Leg1） */
function buildGmapsUrlLeg1FromForm(){
  const v = getFormValues();
  return buildGmapsUrlParam(
    v.origin, v.waypoint, v.depart_date, v.depart_time,
    v.origin_lat, v.origin_lon, v.waypoint_lat, v.waypoint_lon
  );
}

/** 保存共通（kind: "full" | "leg1"）: 座標も同送 */
async function onSaveUrlCommon(kind){
  const msg = $("#action-msg");
  const saveAreaFull = $("#save-result");
  const saveAreaLeg1 = $("#save-result-leg1");
  const v = getFormValues();

  let endpoint, payload, targetArea;
  if (kind === "leg1"){
    endpoint = API_ROUTE_SAVE_LEG1;
    payload = {
      origin: v.origin, waypoint: v.waypoint,
      depart_at: (v.depart_date && v.depart_time) ? `${v.depart_date} ${v.depart_time}` : null,
      origin_lat: v.origin_lat, origin_lon: v.origin_lon,
      waypoint_lat: v.waypoint_lat, waypoint_lon: v.waypoint_lon,
    };
    targetArea = saveAreaLeg1;
    if (!v.origin || !v.waypoint){ alert("出発地と中間地点を入力してください。"); return; }
  }else{
    endpoint = API_ROUTE_SAVE;
    payload = {
      origin: v.origin, destination: v.destination,
      depart_at: (v.depart_date && v.depart_time) ? `${v.depart_date} ${v.depart_time}` : null,
      origin_lat: v.origin_lat, origin_lon: v.origin_lon,
      destination_lat: v.destination_lat, destination_lon: v.destination_lon,
    };
    targetArea = saveAreaFull;
    if (!v.origin || !v.destination){ alert("出発地と到着地を入力してください。"); return; }
  }

  if (msg) msg.textContent = `保存中… (${endpoint})`;

  try{
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (!resp.ok){
      const t = await resp.text();
      if (msg) msg.textContent = `保存に失敗しました（${resp.status}）`;
      if (targetArea) targetArea.value = (t || "").slice(0, 2000);
      return;
    }
    const data = await resp.json();
    if (data?.ok){
      const lines = [];
      lines.push(`保存パス: ${data.saved_path || "(非公開)"}`);
      if (data.url) lines.push(`URL: ${data.url}`);
      if (data.summary) lines.push(`概要: ${data.summary}`);
      if (targetArea) targetArea.value = lines.join("\n");
      if (msg) msg.textContent = "保存しました。";
    }else{
      if (msg) msg.textContent = "保存応答を解釈できませんでした。";
    }
  }catch(err){
    console.error(err);
    if (msg) msg.textContent = "サーバに接続できませんでした。run_server.py の起動状態を確認してください。";
  }
}

/** UIの有効/無効とプレビュー更新 */
function refreshUI(){
  const msg = $("#action-msg");
  const doBtn = $("#do-search");
  const doLeg1Btn = $("#do-search-leg1");
  const saveBtn = $("#save-url");
  const saveLeg1Btn = $("#save-url-leg1");
  const copyBtn = $("#copy-url");
  const copyLeg1Btn = $("#copy-url-leg1");
  const urlInput = $("#url-preview");
  const urlLeg1Input = $("#url-preview-leg1");

  const v = getFormValues();
  const okFull = Boolean(v.origin && v.destination);
  const okLeg1 = Boolean(v.origin && v.waypoint);

  if (doBtn)        doBtn.disabled        = !okFull;
  if (saveBtn)      saveBtn.disabled      = !okFull;
  if (copyBtn)      copyBtn.disabled      = !okFull;
  if (doLeg1Btn)    doLeg1Btn.disabled    = !okLeg1;
  if (saveLeg1Btn)  saveLeg1Btn.disabled  = !okLeg1;
  if (copyLeg1Btn)  copyLeg1Btn.disabled  = !okLeg1;

  const urlFull = okFull ? buildGmapsUrlFromForm() : "";
  const urlLeg1 = okLeg1 ? buildGmapsUrlLeg1FromForm() : "";
  if (urlInput)     urlInput.value     = urlFull || "";
  if (urlLeg1Input) urlLeg1Input.value = urlLeg1 || "";

  if (msg) {
    msg.textContent = okFull
      ? (okLeg1 ? "準備OK：出発→到着／出発→中間 の検索・保存が可能（座標優先）"
                : "準備OK：出発→到着 の検索・保存が可能（座標優先）")
      : "出発地と到着地を入力/選択してください。";
  }
}

/** 候補ポップアップの開閉 */
function openBox(){ if (boxEl){ boxEl.style.display="block"; boxEl.setAttribute("aria-hidden","false"); } }
function closeBox(){
  if (!boxEl) return;
  boxEl.style.display="none"; boxEl.setAttribute("aria-hidden","true");
  if (listEl) listEl.replaceChildren();
  if (summaryEl) summaryEl.textContent = "";
}

/** /api/stops を叩いて候補を描画 */
async function performSearchCore(q, onPick){
  showLoadingBox(q);
  let resp;
  try{
    const params = new URLSearchParams({ q, limit: String(50) });
    resp = await fetch(`${API_STOPS_URL}?${params.toString()}`, { method: "GET" });
  }catch(err){
    console.error(err);
    renderError("検索APIへの接続に失敗しました。サーバが起動しているか確認してください。");
    return;
  }
  if (!resp.ok){
    renderError(`検索に失敗しました（${resp.status}）`);
    return;
  }
  const data = await resp.json();
  const hits = Array.isArray(data?.hits) ? data.hits : [];
  const stats = data?.stats || {};
  renderResults({ query: data?.query ?? q, hits, stats }, onPick);
}

/** ローディング表示 */
function showLoadingBox(q){
  if (!boxEl || !listEl || !summaryEl) return;
  summaryEl.textContent = `「${q}」で検索中…`;
  listEl.replaceChildren();
  listEl.appendChild(document.createElement("li")).className = "loading";
  listEl.lastChild.textContent = "検索中…";
  openBox();
}

/** エラー描画 */
function renderError(msg){
  if (!boxEl || !listEl || !summaryEl) return;
  summaryEl.textContent = "エラー";
  listEl.replaceChildren();
  const li = document.createElement("li");
  li.className = "no-results"; li.textContent = msg;
  listEl.appendChild(li);
  openBox();
}

/** 検索結果の描画（選択時に data-lat/data-lon を入力欄へ保持） */
function renderResults(payload, onPick){
  if (!boxEl || !listEl || !summaryEl) return;
  const { hits = [], stats = {} } = payload || {};
  const files = Number.isFinite(stats.dir_files_considered) ? stats.dir_files_considered : 0;
  const zips  = Number.isFinite(stats.zip_files_considered) ? stats.zip_files_considered : 0;
  const label = `候補 ${hits.length}件 / 解析: dir ${files}, zip ${zips} / 所要 ${stats.elapsed_sec ?? "?"}s`;
  summaryEl.textContent = label;

  listEl.replaceChildren();
  if (hits.length === 0){
    const li = document.createElement("li");
    li.className = "no-results"; li.textContent = "該当なし";
    listEl.appendChild(li);
    openBox(); return;
  }
  for (const item of hits){
    const title = item.name || item.value || "(no title)";
    const pref  = item.pref || "（都道府県 不明）";
    const line  = item.line || "（路線 不明）";
    const hasCoord = Number.isFinite(item?.lat) && Number.isFinite(item?.lon);

    const li = document.createElement("li");
    li.className = "result-item"; li.tabIndex = 0; li.role = "button";
    const t = document.createElement("div"); t.className = "title"; t.textContent = title;
    const s = document.createElement("div"); s.className = "snippet"; s.textContent = `${pref} / ${line}${hasCoord ? " / [座標あり]" : ""}`;
    const m = document.createElement("div"); m.className = "meta";
    m.textContent = [item.source ? `source:${item.source}` : "", item.relpath || "", item.zip ? `zip:${item.zip}` : ""].filter(Boolean).join(" / ");
    li.append(t,s,m);

    li.addEventListener("click", () => { onPick(item); closeBox(); refreshUI(); });
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); li.click(); } });
    listEl.appendChild(li);
  }
  openBox();
}

/** セクション単位の検索配線 */
function wireSearchForSection({ sectionId, buttonSel, formSel, inputSel, previewSel, submitTo }){
  const section = document.getElementById(sectionId);
  if (!section) return;
  const btn = $(buttonSel, section);
  const form = $(formSel, section);
  const inputEl = $(inputSel, section);
  const previewEl = $(previewSel);

  const handler = async (e) => {
    e?.preventDefault?.();
    const rawQuery = (inputEl?.value ?? "").trim();
    if (!rawQuery){ alert("検索キーワードを入力してください。"); return; }
    const q = normalizeQuery(rawQuery);
    await performSearchCore(q, (picked) => {
      const name = picked.name || picked.value || "";
      if (inputEl) {
        inputEl.value = name;
        // 候補が座標を持っていれば data 属性として保持（lon/lat は数値）
        if (Number.isFinite(picked?.lat) && Number.isFinite(picked?.lon)){
          inputEl.dataset.lat = String(picked.lat); // StopItem.lat は緯度
          inputEl.dataset.lon = String(picked.lon); // StopItem.lon は経度
        }else{
          inputEl.dataset.lat = ""; inputEl.dataset.lon = "";
        }
        inputEl.dataset.source = picked.source || "";
      }
      if (previewEl){
        const pref = picked.pref || "（都道府県 不明）";
        const line = picked.line || "（路線 不明）";
        const hasC = Number.isFinite(picked?.lat) && Number.isFinite(picked?.lon);
        previewEl.textContent = `選択: ${name}  /  ${pref} / ${line}${hasC ? " / [座標あり]" : ""}`;
      }
    });
  };

  if (btn)  btn.addEventListener("click", handler);
  form?.addEventListener("submit", (e) => { e.preventDefault(); onSubmitSearch(e, submitTo || "#do-search"); });
  inputEl?.addEventListener("input", () => { if (previewEl) previewEl.textContent = ""; inputEl.dataset.lat=""; inputEl.dataset.lon=""; });
}

/** クリップボードコピー */
async function copyText(t){
  if (!t) return;
  try { await navigator.clipboard.writeText(t); toast("URLをコピーしました。"); }
  catch { fallbackCopyText(t); }
}
function fallbackCopyText(text){
  try{
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position="fixed"; ta.style.left="-9999px";
    document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
    toast("URLをコピーしました。");
  }catch{ alert("コピーに失敗しました。"); }
}
function toast(message){
  const t = document.createElement("div");
  t.textContent = message;
  Object.assign(t.style, {
    position: "fixed", right: "16px", top: "16px",
    padding: "10px 14px", background: "#333", color: "#fff",
    borderRadius: "8px", fontSize: "12px", zIndex: 10000, opacity: "0.95"
  });
  document.body.appendChild(t);
  setTimeout(()=>{ t.remove(); }, 1600);
}
