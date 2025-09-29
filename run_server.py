# -*- coding: utf-8 -*-
"""
run_server.py
目的:
  - プロジェクトルート/data を再帰的に走査して駅/バス停データをインデックス化。
  - 元データ(解凍物やZIP)は無改変のまま利用する。
  - /api/stops に対して「駅名orバス停名」「都道府県」「路線名」をセットで返す。
  - 参考の汎用全文検索 /api/search も提供（既存互換の簡易版）。
  - 静的ファイル (input.html, app.js, style.css 等) を配信。
  - ★ 追加: サーバ起動時に既定でブラウザを自動起動（/ → input.html）。--no-open で無効化可能。

できること(詳細):
  - data配下を再帰的に巡回。ディレクトリ/ZIPの両方を走査。
  - 駅(N05-**_GML の Station2.geojson 等)、バス停(P11-**_GML 由来のGeoJSON等)、
    および stops.*.xml を柔軟に読み取り、[name/pref/line] を抽出・インデックス化。
  - 都道府県(pref)は:
      1) プロパティ内に都道府県名が含まれていればそれを採用
      2) XML内に <pref>, <prefecture>, <都道府県> 等があれば採用
      3) 上記が無い場合は不明("")のまま返す（※データを変えない方針のため）
    ※ より厳密な都道府県判定（座標→逆ジオコーディング）は外部データが必要なため未使用。
  - /api/stops?q=新宿&limit=50 などでヒットを JSON 返却。hits の各要素は:
      { name, pref, line, value(=name), snippet("pref / line"), source, relpath, zip }。
  - /api/stops/reindex で手動再構築（GET/POST）。
  - /api/search は既存のコンソール表示仕様に合わせた簡易実装（ファイル名・本文テキストを対象）。
  - / は input.html を返す。静的: /app.js /style.css
  - ★ ブラウザ自動起動（既定ON / 起動待ち合わせあり / 失敗してもサーバは継続）

このモジュールの目的:
  - 「中継地点あり」など経路UIの出発地/到着地候補を、政府公開のN05/P11データを
    そのまま読み取ってサジェストする基盤APIを提供すること。
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import zipfile
import threading  # ★ 追加: 非同期ブラウザ起動用
import webbrowser # ★ 追加: 既定ブラウザ起動
import socket     # ★ 追加: ポート開通確認
import argparse   # ★ 追加: CLIオプション

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from flask import Flask, jsonify, request, send_from_directory

# =========================
# 基本設定 変数
# =========================

# プロジェクトルート
PROJECT_ROOT: Path = Path(__file__).resolve().parent
# データルート（再帰的に検索）
DATA_ROOT: Path = PROJECT_ROOT / "data"
# 静的配信ルート（本プロジェクトではルート直下に input.html/app.js/style.css を置いている）
VIEW_ROOT: Path = PROJECT_ROOT

# 既知の都道府県名（抽出用のリスト）
PREF_LIST: List[str] = [
    "北海道","青森県","岩手県","宮城県","秋田県","山形県","福島県",
    "茨城県","栃木県","群馬県","埼玉県","千葉県","東京都","神奈川県",
    "新潟県","富山県","石川県","福井県","山梨県","長野県",
    "岐阜県","静岡県","愛知県","三重県",
    "滋賀県","京都府","大阪府","兵庫県","奈良県","和歌山県",
    "鳥取県","島根県","岡山県","広島県","山口県",
    "徳島県","香川県","愛媛県","高知県",
    "福岡県","佐賀県","長崎県","熊本県","大分県","宮崎県","鹿児島県",
    "沖縄県",
]

# =========================
# 汎用ユーティリティ
# =========================

def norm_text(s: str) -> str:
    """簡易正規化: 全角英数→半角 / 連続空白を1つに / 前後空白除去"""
    if not s:
        return ""
    s2 = re.sub(r"[！-～]", lambda m: chr(ord(m.group(0)) - 0xFEE0), s)
    return re.sub(r"\s+", " ", s2).strip()


def any_contains_pref(values: Iterable[str]) -> str:
    """与えられた文字列群のどれかに都道府県名が含まれていれば最初の一致を返す。無ければ ""。"""
    for v in values:
        if not isinstance(v, str):
            continue
        for p in PREF_LIST:
            if p in v:
                return p
        # 値自体が都道府県名と完全一致する場合も拾う
        if v in PREF_LIST:
            return v
    return ""


# =========================
# データ構造
# =========================

@dataclass
class StopItem:
    """駅/バス停 1件分の正規化済みレコード"""
    name: str          # 駅名/バス停名
    pref: str          # 都道府県名（不明なら ""）
    line: str          # 路線名（不明なら ""）
    source: str        # "rail(n05)" / "bus(p11)" / "xml" など
    relpath: str       # 実体ファイルの相対パス（ZIP内は "zip:path#inner"）
    zip: Optional[str] # ZIPパス（ディレクトリ内実体なら None）
    lon: Optional[float] = None  # 参考: 座標 (あれば)
    lat: Optional[float] = None

    @property
    def value(self) -> str:
        """フロント表示の主タイトルとして name を返す（互換用）。"""
        return self.name

    @property
    def snippet(self) -> str:
        """フロントで見やすい補足（pref / line）。"""
        p = self.pref or "（都道府県 不明）"
        l = self.line or "（路線 不明）"
        return f"{p} / {l}"


# =========================
# インデクサ（駅/バス停）
# =========================

class StopsIndexer:
    """data配下(再帰 & ZIP対応)を走査し、StopItem のリストを作る"""

    def __init__(self, data_root: Path):
        # 変数: データルート
        self.data_root: Path = data_root
        # 変数: 構築済みの全ストップ
        self.items: List[StopItem] = []
        # 統計: 走査ファイル数
        self.dir_files_considered: int = 0
        self.zip_files_considered: int = 0

    # ---------- 公開メソッド ----------

    def build(self) -> None:
        """インデックス構築（毎回リセットして再構築）"""
        self.items = []
        self.dir_files_considered = 0
        self.zip_files_considered = 0

        # ディレクトリ直走査
        for path in self._iter_files(self.data_root):
            self.dir_files_considered += 1
            self._try_ingest_path(path)

        # ZIPも含めて走査
        for zpath in self._iter_zip_files(self.data_root):
            self.zip_files_considered += 1
            self._try_ingest_zip(zpath)

    def search(self, q: str, limit: int = 50) -> List[StopItem]:
        """シンプル検索: name/line/pref のいずれかにクエリ語が含まれるものを返す"""
        qn = norm_text(q)
        if not qn:
            return []
        tokens = qn.lower().split(" ")

        def score(item: StopItem) -> Tuple[int, int, int]:
            # スコアリング: name優先、pref一致、line一致
            name_l = item.name.lower()
            pref_l = item.pref.lower()
            line_l = item.line.lower()
            hit_name = sum(t in name_l for t in tokens)
            hit_pref = sum(t in pref_l for t in tokens)
            hit_line = sum(t in line_l for t in tokens)
            return (-hit_name, -hit_pref, -hit_line)  # 小さい方が高評価

        hits = [
            it for it in self.items
            if all(
                (t in it.name.lower()) or (t in (it.line or "").lower()) or (t in (it.pref or "").lower())
                for t in tokens
            )
        ]
        hits.sort(key=score)
        return hits[:limit]

    # ---------- 内部: 走査 ----------

    def _iter_files(self, root: Path) -> Iterable[Path]:
        """data配下の通常ファイルを再帰列挙"""
        for p in root.rglob("*"):
            if p.is_file():
                yield p

    def _iter_zip_files(self, root: Path) -> Iterable[Path]:
        """data配下のZIPファイルを再帰列挙"""
        for p in root.rglob("*.zip"):
            if p.is_file():
                yield p

    # ---------- 内部: ingest ----------

    def _try_ingest_path(self, path: Path) -> None:
        """単体ファイルを読み、StopItemを追加（判別は拡張子/名前で緩やかに）"""
        name = path.name.lower()
        try:
            if name.endswith(".geojson"):
                self._ingest_geojson_file(path, zip_ctx=None)
            elif name.endswith(".json"):
                self._ingest_geojson_file(path, zip_ctx=None)  # JSONでもGeoJSON想定を緩く許容
            elif name.endswith(".xml") and "stops" in name:
                self._ingest_stops_xml(path)
            # Shapefile等は対象外（解凍物のGeoJSON/作成済XML優先）
        except Exception:
            # 解析失敗はスキップ（ログはコンソールへ）
            print(f"[WARN] ingest fail: {path}", file=sys.stderr)

    def _try_ingest_zip(self, zpath: Path) -> None:
        """ZIP内の GeoJSON / JSON / stops.*.xml を探索して取り込む"""
        try:
            with zipfile.ZipFile(zpath, "r") as zf:
                for info in zf.infolist():
                    nm = info.filename
                    low = nm.lower()
                    if not (low.endswith(".geojson") or low.endswith(".json") or (low.endswith(".xml") and "stops" in low)):
                        continue
                    with zf.open(info, "r") as fp:
                        data = fp.read()
                    if low.endswith(".xml"):
                        self._ingest_stops_xml(io.BytesIO(data), zip_ctx=zpath, inner=nm)
                    else:
                        self._ingest_geojson_bytes(data, rel=f"{zpath.name}#{nm}", zip_ctx=zpath)
        except Exception:
            print(f"[WARN] zip ingest fail: {zpath}", file=sys.stderr)

    # ---------- 内部: パーサ ----------

    def _ingest_geojson_file(self, path: Path, zip_ctx: Optional[Path]) -> None:
        """GeoJSONファイルを開いて ingest"""
        with path.open("r", encoding="utf-8") as f:
            data = f.read().encode("utf-8")
        rel = str(path.relative_to(PROJECT_ROOT))
        self._ingest_geojson_bytes(data, rel=rel, zip_ctx=zip_ctx)

    def _ingest_geojson_bytes(self, raw: bytes, rel: str, zip_ctx: Optional[Path]) -> None:
        """GeoJSONの bytes を ingest"""
        obj = json.loads(raw.decode("utf-8", errors="ignore"))
        feats = obj.get("features") or []
        for ft in feats:
            props = ft.get("properties") or {}
            geom = ft.get("geometry") or {}
            gtype = (geom.get("type") or "").lower()

            # 座標を拾えるなら取得
            lon = lat = None
            if gtype == "point":
                coords = geom.get("coordinates") or []
                if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                    lon, lat = float(coords[0]), float(coords[1])

            # --- N05 (駅) と P11 (バス停) を緩く判別して抽出 ---
            # N05 駅: N05_011 = 駅名, N05_002 = 路線名, N05_003 = 事業者（都道府県を含むことがある）
            n05_name = props.get("N05_011")
            n05_line = props.get("N05_002")
            n05_oper = props.get("N05_003")

            # P11 バス停（公開形式差があるため、キー名は柔軟に探索）
            # よくある候補: "停留所名", "バス停名", "名称", "name" など
            bus_name = _first_str(props, ["停留所名", "バス停名", "名称", "name", "P11_011", "P11_010"])
            bus_line = _first_str(props, ["路線名", "系統名", "line", "P11_002", "P11_003"])

            # 都道府県はプロパティの値群から抽出（含まれていれば採用）
            found_pref = any_contains_pref([str(v) for v in props.values()] + [str(n05_oper or "")])

            if n05_name:  # 駅（N05）
                self.items.append(StopItem(
                    name=str(n05_name),
                    pref=found_pref,
                    line=str(n05_line or ""),
                    source="rail(n05)",
                    relpath=rel,
                    zip=str(zip_ctx) if zip_ctx else None,
                    lon=lon, lat=lat
                ))
            elif bus_name:  # バス(P11)
                self.items.append(StopItem(
                    name=str(bus_name),
                    pref=found_pref,
                    line=str(bus_line or ""),
                    source="bus(p11)",
                    relpath=rel,
                    zip=str(zip_ctx) if zip_ctx else None,
                    lon=lon, lat=lat
                ))
            # それ以外のGeoJSONは無視

    def _ingest_stops_xml(self, path_or_bytes: Union[Path, io.BytesIO], zip_ctx: Optional[Path] = None, inner: Optional[str] = None) -> None:
        """stops.*.xml を緩く解析（<name>, <pref|prefecture|都道府県>, <line|路線|系統> を拾う）"""
        import xml.etree.ElementTree as ET

        if isinstance(path_or_bytes, Path):
            rel = str(path_or_bytes.relative_to(PROJECT_ROOT))
            with path_or_bytes.open("rb") as f:
                raw = f.read()
        else:
            rel = f"{zip_ctx.name}#{inner}" if (zip_ctx and inner) else "(zip)"
            raw = path_or_bytes.getvalue()

        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            # XMLとして読めなければあきらめる
            print(f"[WARN] XML parse error: {rel}", file=sys.stderr)
            return

        # stop/station/busStop 等を包括的に拾う
        candidates = list(root.iter())
        for node in candidates:
            # 子に <name> があり、かつ同じ親配下に <line> などがある構造を想定
            nm = _first_text(node, ["name", "駅名", "停留所名"])
            if not nm:
                continue
            pref = _first_text(node, ["pref", "prefecture", "都道府県"])
            line = _first_text(node, ["line", "路線", "系統"])
            # さらに親/子を少し広めに探索（ルーズに拾う）
            if not line:
                for near in list(node.iter()):
                    line = _first_text(near, ["line", "路線", "系統"])
                    if line:
                        break
            if not pref:
                # 直近の兄弟や親のテキストから都道府県っぽいものを抽出
                near_vals = []
                parent = node if node is not None else None
                if parent is not None:
                    for near in list(parent.iter()):
                        if near.text:
                            near_vals.append(near.text.strip())
                    pref = any_contains_pref(near_vals)

            self.items.append(StopItem(
                name=nm, pref=pref or "", line=line or "",
                source="xml",
                relpath=rel, zip=str(zip_ctx) if zip_ctx else None
            ))


def _first_str(d: Dict[str, Any], keys: List[str]) -> Optional[str]:
    """辞書 d から keys のいずれかに一致するキーの最初の str を返す"""
    for k in keys:
        if k in d and isinstance(d[k], str) and d[k].strip():
            return d[k].strip()
    return None


def _first_text(node: Any, tag_names: List[str]) -> Optional[str]:
    """XMLノードで tag_names のいずれかの直下要素テキストを返す"""
    for t in tag_names:
        el = node.find(t)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return None


# =========================
# 全文検索 (簡易)
# =========================

class GrepLikeSearch:
    """/api/search 用の簡易全文検索（ファイル名・テキスト本文を対象）。
       ※ デモ用の軽量実装。大規模ではインデクサ導入推奨。
    """
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.dir_files_considered = 0
        self.zip_files_considered = 0

    def search(self, q: str, limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        qn = norm_text(q).lower()
        t0 = time.time()
        hits: List[Dict[str, Any]] = []
        self.dir_files_considered = 0
        self.zip_files_considered = 0

        # 通常ファイル
        for p in self.data_root.rglob("*"):
            if not p.is_file():
                continue
            self.dir_files_considered += 1
            # ファイル名ヒット
            if qn in p.name.lower():
                hits.append({
                    "value": p.name, "relpath": str(p.relative_to(PROJECT_ROOT)),
                    "snippet": "(filename hit)", "source": "dir"
                })
                if len(hits) >= limit: break
            # テキスト本文（UTF-8前提で緩く）
            try:
                txt = p.read_text("utf-8", errors="ignore")
            except Exception:
                continue
            if qn in txt.lower():
                line = _one_line_around(txt, q)
                hits.append({
                    "value": p.name, "relpath": str(p.relative_to(PROJECT_ROOT)),
                    "snippet": line, "source": "dir"
                })
                if len(hits) >= limit: break

        # ZIP 内も軽く見る
        if len(hits) < limit:
            for zpath in self.data_root.rglob("*.zip"):
                if not zpath.is_file():
                    continue
                self.zip_files_considered += 1
                try:
                    with zipfile.ZipFile(zpath, "r") as zf:
                        for info in zf.infolist():
                            nm = info.filename
                            if qn in nm.lower():
                                hits.append({
                                    "value": nm, "relpath": f"{zpath.name}#{nm}",
                                    "snippet": "(filename hit)", "source": "zip", "zip": str(zpath)
                                })
                                if len(hits) >= limit: break
                            with zf.open(info, "r") as fp:
                                raw = fp.read().decode("utf-8", errors="ignore")
                            if qn in raw.lower():
                                hits.append({
                                    "value": nm, "relpath": f"{zpath.name}#{nm}",
                                    "snippet": _one_line_around(raw, q), "source": "zip", "zip": str(zpath)
                                })
                                if len(hits) >= limit: break
                except Exception:
                    print(f"[WARN] search zip fail: {zpath}", file=sys.stderr)
                if len(hits) >= limit:
                    break

        elapsed = round(time.time() - t0, 3)
        stats = {
            "kind": "grep",
            "elapsed_sec": elapsed,
            "dir_files_considered": self.dir_files_considered,
            "zip_files_considered": self.zip_files_considered,
        }
        return hits[:limit], stats


def _one_line_around(txt: str, q: str) -> str:
    """ヒット周辺の1行を返す(見やすさ用)"""
    qn = norm_text(q)
    for line in txt.splitlines():
        if qn.lower() in norm_text(line).lower():
            return line.strip()[:200]
    return ""


# =========================
# Flask アプリ
# =========================

app = Flask(__name__, static_folder=None)

# グローバル: インデクサ（遅延構築）
STOP_INDEXER: Optional[StopsIndexer] = None
GREP_SEARCH: Optional[GrepLikeSearch] = None


def ensure_indexers() -> None:
    """初回アクセス時にインデクサを構築"""
    global STOP_INDEXER, GREP_SEARCH
    if STOP_INDEXER is None:
        STOP_INDEXER = StopsIndexer(DATA_ROOT)
        t0 = time.time()
        STOP_INDEXER.build()
        print(f"[INDEX] stops built in {time.time()-t0:.2f}s, items={len(STOP_INDEXER.items)}")
    if GREP_SEARCH is None:
        GREP_SEARCH = GrepLikeSearch(DATA_ROOT)


# ---------- 静的配信 ----------

@app.route("/")
def index_html():
    """トップ: input.html を返す"""
    return send_from_directory(str(VIEW_ROOT), "input.html")


@app.route("/app.js")
def serve_js():
    return send_from_directory(str(VIEW_ROOT), "app.js")


@app.route("/style.css")
def serve_css():
    return send_from_directory(str(VIEW_ROOT), "style.css")


# ---------- API: stops ----------

@app.route("/api/stops", methods=["GET", "POST"])
def api_stops():
    """駅/バス停サジェスト: q, limit を受け取り、[name/pref/line] のセットを返す"""
    ensure_indexers()
    q = request.values.get("q", "")
    limit = int(request.values.get("limit", "50") or "50")
    # 'force' は互換目的で受けるのみ
    t0 = time.time()
    hits: List[StopItem] = STOP_INDEXER.search(q, limit=limit) if STOP_INDEXER else []
    elapsed = round(time.time() - t0, 3)

    # フロントの既存レンダリングに合わせた汎用アイテム形式
    out_hits: List[Dict[str, Any]] = []
    for it in hits:
        d = asdict(it)
        d["value"] = it.value
        d["snippet"] = it.snippet
        out_hits.append(d)

    stats = {
        "kind": "stops",
        "elapsed_sec": elapsed,
        "dir_files_considered": STOP_INDEXER.dir_files_considered if STOP_INDEXER else 0,
        "zip_files_considered": STOP_INDEXER.zip_files_considered if STOP_INDEXER else 0,
        "total_items": len(STOP_INDEXER.items) if STOP_INDEXER else 0,
    }
    return jsonify({"query": q, "hits": out_hits, "stats": stats})


@app.route("/api/stops/reindex", methods=["GET", "POST"])
def api_stops_reindex():
    """駅/バス停インデックスの再構築"""
    global STOP_INDEXER
    STOP_INDEXER = StopsIndexer(DATA_ROOT)
    t0 = time.time()
    STOP_INDEXER.build()
    elapsed = round(time.time() - t0, 3)
    return jsonify({
        "ok": True,
        "elapsed_sec": elapsed,
        "items": len(STOP_INDEXER.items),
        "dir_files_considered": STOP_INDEXER.dir_files_considered,
        "zip_files_considered": STOP_INDEXER.zip_files_considered,
    })


# ---------- API: 汎用検索 (簡易) ----------

@app.route("/api/search", methods=["GET", "POST"])
def api_search():
    """簡易全文検索: q, limit, mode/scopeはダミー互換"""
    ensure_indexers()
    q = request.values.get("q", "")
    limit = int(request.values.get("limit", "50") or "50")
    hits, stats = GREP_SEARCH.search(q, limit=limit) if GREP_SEARCH else ([], {})
    return jsonify({"query": q, "hits": hits, "stats": stats})


# ---------- /api/reindex (互換) ----------

@app.route("/api/reindex", methods=["GET", "POST"])
def api_reindex():
    """簡易全文検索の再初期化（互換API）"""
    global GREP_SEARCH
    GREP_SEARCH = GrepLikeSearch(DATA_ROOT)
    return jsonify({"ok": True})


# =========================
# ブラウザ自動起動 まわり（復元機能）
# =========================

def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    """指定ホスト/ポートへ接続できるか簡易確認"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _launch_browser_when_ready(url: str, probe_host: str, port: int, delay: float = 0.8, timeout: float = 15.0) -> None:
    """
    サーバが起動して受け付け可能になったら既定ブラウザで URL を開く。
    - delay: 初期待機秒
    - timeout: 最大待機秒（超えたら諦める）
    """
    try:
        time.sleep(max(0.0, delay))
        start = time.time()

        # 0.0.0.0 のような場合は 127.0.0.1 で疎通確認する
        probe = "127.0.0.1" if probe_host in ("0.0.0.0", "::") else probe_host

        while time.time() - start < timeout:
            if _is_port_open(probe, port):
                webbrowser.open(url)
                return
            time.sleep(0.25)
        # タイムアウトしてもサーバ自体は起動しているので何もしない
    except Exception:
        # 失敗してもサーバは継続させる
        pass


# ---------- メイン ----------

def _print_banner(host: str, port: int):
    print("====================================")
    print(" Local Web Server  (./data 限定 & 再帰検索)")
    print("====================================")
    print(f" Project : {PROJECT_ROOT}")
    print(f" Serve   : {VIEW_ROOT}  ( / → input.html )")
    print(f" Roots   : [{DATA_ROOT}]")
    print(" API     : GET /api/search?q=...&mode=dir|zip|both&scope=filename|content|both&limit=N")
    print("           POST /api/search (JSON/form: q,mode,scope,limit,force)")
    print("           GET/POST /api/reindex")
    print("           GET/POST /api/stops?q=駅名&limit=N[&force=1]")
    print("           GET/POST /api/stops/reindex")
    print(" Stop    : Ctrl + C")
    print("------------------------------------")
    print(f" Host/Port : {host}:{port}")
    print(f" Open URL  : http://127.0.0.1:{port}/  （自動起動: 既定ON / --no-open で無効）")
    print("====================================")


if __name__ == "__main__":
    # CLI オプション
    parser = argparse.ArgumentParser(description="Local Web Server for station/bus stop search")
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument("--no-open", action="store_true", help="do NOT auto-open default browser")
    parser.add_argument("--open-delay", type=float, default=0.8, help="seconds to wait before probing and opening browser")
    args = parser.parse_args()

    host = args.host
    port = args.port
    auto_open = not args.no_open
    open_delay = max(0.0, args.open_delay)

    _print_banner(host, port)

    # ★ 復元: ブラウザ自動起動（デーモンスレッド）
    if auto_open:
        url = f"http://127.0.0.1:{port}/"  # 表示用は 127.0.0.1 を優先
        th = threading.Thread(
            target=_launch_browser_when_ready,
            args=(url, host, port, open_delay),
            daemon=True,
        )
        th.start()

    # Flask 開始
    # 注意: Flask 標準サーバは開発向け。社内/本番で常用する場合は waitress/gunicorn 等を推奨。
    from werkzeug.serving import WSGIRequestHandler
    # ログの簡素化（任意）
    try:
        WSGIRequestHandler.protocol_version = "HTTP/1.1"
    except Exception:
        pass

    # 実行
    app.run(host=host, port=port, debug=False)
