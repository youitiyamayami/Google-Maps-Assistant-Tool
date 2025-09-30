# -*- coding: utf-8 -*-
"""
run_server.py（座標優先URL対応 + P11 GML 取込）
目的:
  - data を再帰・ZIP対応で走査し駅/バス停をインデックス化（/api/stops）
      * N05(駅) GeoJSON: geometry.coordinates=[lon,lat]
      * P11(バス) GML:   ksj:BusStop ⇔ gml:Point(gml:pos="lat lon") を照合し座標取得
  - 簡易全文検索（/api/search）
  - 静的配信（/ → input.html ほか）
  - URL生成/保存API（座標があれば lat,lng を優先して URL を生成）
      * POST /api/route/save        … 出発→到着
      * POST /api/route/save_leg1   … 出発→中間(Leg1)
"""

from __future__ import annotations

import io, json, re, sys, time, zipfile, threading, webbrowser, socket, argparse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from datetime import datetime
import urllib.parse
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, request, send_from_directory, abort

# =========================
# 基本設定
# =========================
PROJECT_ROOT: Path = Path(__file__).resolve().parent
DATA_ROOT: Path = PROJECT_ROOT / "data"
VIEW_ROOT: Path = PROJECT_ROOT
OUTPUT_DIR: Path = PROJECT_ROOT / "output"

# 都道府県抽出用の簡易リスト
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
# ユーティリティ
# =========================
def norm_text(s: str) -> str:
    """全角英数→半角、連続空白圧縮、前後空白除去。"""
    if not s:
        return ""
    s2 = re.sub(r"[！-～]", lambda m: chr(ord(m.group(0)) - 0xFEE0), s)
    return re.sub(r"\s+", " ", s2).strip()

def any_contains_pref(values: Iterable[str]) -> str:
    """与えた文字列群の中に都道府県名があれば最初の一致を返す。"""
    for v in values:
        if not isinstance(v, str):
            continue
        for p in PREF_LIST:
            if p in v:
                return p
        if v in PREF_LIST:
            return v
    return ""

def _localname(tag: str) -> str:
    """XML タグのローカル名（{ns}name → name）"""
    return tag.split("}")[-1] if "}" in tag else tag

def _num(v: Any) -> Optional[float]:
    """float 変換（失敗時 None）"""
    try:
        f = float(v)
        if f == float("inf") or f == float("-inf"):
            return None
        return f
    except Exception:
        return None

# =========================
# データ構造
# =========================
@dataclass
class StopItem:
    """駅/バス停 1件分の正規化済みレコード"""
    name: str
    pref: str
    line: str
    source: str
    relpath: str
    zip: Optional[str]
    lon: Optional[float] = None   # 経度 (x)
    lat: Optional[float] = None   # 緯度 (y)

    @property
    def value(self) -> str:
        return self.name

    @property
    def snippet(self) -> str:
        return f"{self.pref or '（都道府県 不明）'} / {self.line or '（路線 不明）'}"

# =========================
# インデクサ
# =========================
class StopsIndexer:
    """data配下(再帰 & ZIP対応)を走査し、StopItem のリストを構築"""

    def __init__(self, data_root: Path):
        self.data_root: Path = data_root
        self.items: List[StopItem] = []
        self.dir_files_considered: int = 0
        self.zip_files_considered: int = 0

    def build(self) -> None:
        """インデックス構築（毎回リセット）"""
        self.items.clear()
        self.dir_files_considered = 0
        self.zip_files_considered = 0

        # 通常ファイル
        for path in self._iter_files(self.data_root):
            self.dir_files_considered += 1
            self._try_ingest_path(path)

        # ZIP 内
        for zpath in self._iter_zip_files(self.data_root):
            self.zip_files_considered += 1
            self._try_ingest_zip(zpath)

    def search(self, q: str, limit: int = 50) -> List[StopItem]:
        """name/line/pref に q のトークンがすべて含まれるものを返す（簡易）"""
        qn = norm_text(q)
        if not qn:
            return []
        tokens = qn.lower().split()

        def score(item: StopItem) -> Tuple[int, int, int]:
            name_l = item.name.lower()
            pref_l = item.pref.lower()
            line_l = item.line.lower()
            hit_name = sum(t in name_l for t in tokens)
            hit_pref = sum(t in pref_l for t in tokens)
            hit_line = sum(t in line_l for t in tokens)
            return (-hit_name, -hit_pref, -hit_line)  # ヒット数の降順

        hits = [
            it for it in self.items
            if all((t in it.name.lower()) or (t in (it.line or '').lower()) or (t in (it.pref or '').lower())
                   for t in tokens)
        ]
        hits.sort(key=score)
        return hits[:limit]

    # ---- 走査 ----
    def _iter_files(self, root: Path) -> Iterable[Path]:
        for p in root.rglob("*"):
            if p.is_file():
                yield p

    def _iter_zip_files(self, root: Path) -> Iterable[Path]:
        for p in root.rglob("*.zip"):
            if p.is_file():
                yield p

    # ---- ingest ----
    def _try_ingest_path(self, path: Path) -> None:
        low = path.name.lower()
        try:
            if low.endswith(".geojson") or low.endswith(".json"):
                self._ingest_geojson_file(path, zip_ctx=None)
            elif low.endswith(".gml") or low.endswith(".xml"):
                self._ingest_p11_gml(path, zip_ctx=None)  # GML/XML を汎用に処理
        except Exception:
            print(f"[WARN] ingest fail: {path}", file=sys.stderr)

    def _try_ingest_zip(self, zpath: Path) -> None:
        try:
            with zipfile.ZipFile(zpath, "r") as zf:
                for info in zf.infolist():
                    low = info.filename.lower()
                    if not (low.endswith(".geojson") or low.endswith(".json") or low.endswith(".gml") or low.endswith(".xml")):
                        continue
                    with zf.open(info, "r") as fp:
                        data = fp.read()
                    if low.endswith(".geojson") or low.endswith(".json"):
                        self._ingest_geojson_bytes(data, rel=f"{zpath.name}#{info.filename}", zip_ctx=zpath)
                    else:
                        self._ingest_p11_gml(io.BytesIO(data), zip_ctx=zpath, inner=info.filename)
        except Exception:
            print(f"[WARN] zip ingest fail: {zpath}", file=sys.stderr)

    # ---- N05(駅) GeoJSON ----
    def _ingest_geojson_file(self, path: Path, zip_ctx: Optional[Path]) -> None:
        with path.open("r", encoding="utf-8") as f:
            data = f.read().encode("utf-8")
        rel = str(path.relative_to(PROJECT_ROOT))
        self._ingest_geojson_bytes(data, rel=rel, zip_ctx=zip_ctx)

    def _ingest_geojson_bytes(self, raw: bytes, rel: str, zip_ctx: Optional[Path]) -> None:
        obj = json.loads(raw.decode("utf-8", errors="ignore"))
        feats = obj.get("features") or []
        for ft in feats:
            props = ft.get("properties") or {}
            geom = ft.get("geometry") or {}
            gtype = (geom.get("type") or "").lower()

            lon = lat = None
            if gtype == "point":
                coords = geom.get("coordinates") or []
                if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                    lon, lat = _num(coords[0]), _num(coords[1])

            n05_name = props.get("N05_011")  # 駅名
            n05_line = props.get("N05_002")  # 路線
            n05_oper = props.get("N05_003")  # 事業者（都道府県抽出の手がかり）

            found_pref = any_contains_pref([str(v) for v in props.values()] + [str(n05_oper or "")])

            if n05_name:
                self.items.append(StopItem(
                    name=str(n05_name), pref=found_pref, line=str(n05_line or ""),
                    source="rail(n05)", relpath=rel, zip=str(zip_ctx) if zip_ctx else None,
                    lon=lon, lat=lat
                ))

    # ---- P11(バス) GML/XML ----
    def _ingest_p11_gml(self, path_or_bytes: Union[Path, io.BytesIO],
                         zip_ctx: Optional[Path] = None, inner: Optional[str] = None) -> None:
        """
        P11 GML の想定:
          <ksj:BusStop gml:id="bs529">
            <ksj:loc xlink:href="#n529"/>
            <ksj:bsn>停留所名</ksj:bsn>
            ...
          </ksj:BusStop>
          <gml:Point gml:id="n529">
            <gml:pos>35.79068787 139.53741184</gml:pos>  ← lat lon の順
          </gml:Point>
        """
        if isinstance(path_or_bytes, Path):
            rel = str(path_or_bytes.relative_to(PROJECT_ROOT))
            try:
                raw = path_or_bytes.read_bytes()
            except Exception:
                return
        else:
            rel = f"{zip_ctx.name}#{inner}" if (zip_ctx and inner) else "(zip)"
            raw = path_or_bytes.getvalue()

        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return

        # gml:Point を先に集約（gml:id → (lon,lat)）
        id2coord: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        for node in root.iter():
            if _localname(node.tag) != "Point":
                continue
            gid = node.attrib.get("{http://www.opengis.net/gml/3.2}id") \
               or node.attrib.get("gml:id") \
               or node.attrib.get("id")
            if not gid:
                continue
            pos_text = None
            for ch in node:
                if _localname(ch.tag) == "pos" and (ch.text or "").strip():
                    pos_text = ch.text.strip()
                    break
            if not pos_text:
                continue
            # P11 GML の pos は "lat lon"
            try:
                lat_s, lon_s = pos_text.split()
                lat, lon = _num(lat_s), _num(lon_s)
            except Exception:
                lat = lon = None
            id2coord[str(gid)] = (lon, lat)

        # BusStop を走査して StopItem を生成
        for node in root.iter():
            if _localname(node.tag) != "BusStop":
                continue
            # 名称/事業者
            name = None; oper = None
            ref_id = None
            for ch in node:
                ln = _localname(ch.tag)
                if ln == "bsn" and (ch.text or "").strip():
                    name = ch.text.strip()
                elif ln == "boc" and (ch.text or "").strip():
                    oper = ch.text.strip()
                elif ln == "loc":
                    ref = ch.attrib.get("{http://www.w3.org/1999/xlink}href") or ch.attrib.get("href")
                    if ref and ref.startswith("#"):
                        ref_id = ref[1:]
            if not name:
                continue
            lon = lat = None
            if ref_id and ref_id in id2coord:
                lon, lat = id2coord[ref_id]
            # 都道府県は BusStop の近傍テキストから推定（なければ空）
            near_vals: List[str] = []
            for ch in node.iter():
                if ch.text and ch.text.strip():
                    near_vals.append(ch.text.strip())
            pref = any_contains_pref(near_vals + [oper or ""])

            self.items.append(StopItem(
                name=name, pref=pref or "", line="", source="bus(p11gml)",
                relpath=rel, zip=str(zip_ctx) if zip_ctx else None,
                lon=lon, lat=lat
            ))

# =========================
# 簡易全文検索（省略…前回同様）
# =========================
class GrepLikeSearch:
    """/api/search 用の簡易全文検索（名前/内容を走査）"""
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
            if qn in p.name.lower():
                hits.append({"value": p.name, "relpath": str(p.relative_to(PROJECT_ROOT)),
                             "snippet": "(filename hit)", "source": "dir"})
                if len(hits) >= limit: break
            try:
                txt = p.read_text("utf-8", errors="ignore")
            except Exception:
                continue
            if qn in txt.lower():
                line = _one_line_around(txt, q)
                hits.append({"value": p.name, "relpath": str(p.relative_to(PROJECT_ROOT)),
                             "snippet": line, "source": "dir"})
                if len(hits) >= limit: break

        # ZIP 内
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
                                hits.append({"value": nm, "relpath": f"{zpath.name}#{nm}",
                                             "snippet": "(filename hit)", "source": "zip", "zip": str(zpath)})
                                if len(hits) >= limit: break
                            with zf.open(info, "r") as fp:
                                raw = fp.read().decode("utf-8", errors="ignore")
                            if qn in raw.lower():
                                hits.append({"value": nm, "relpath": f"{zpath.name}#{nm}",
                                             "snippet": _one_line_around(raw, q), "source": "zip", "zip": str(zpath)})
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
    qn = norm_text(q)
    for line in txt.splitlines():
        if qn.lower() in norm_text(line).lower():
            return line.strip()[:200]
    return ""

# =========================
# URL生成・保存 共通
# =========================
def ensure_output_dir() -> None:
    """output ディレクトリを用意（存在しなければ作成）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def _to_epoch_if_possible(depart_at: Optional[str]) -> Optional[int]:
    """'YYYY-MM-DD HH:MM' を epoch 秒へ。失敗時は None。"""
    if not depart_at:
        return None
    try:
        dt = datetime.strptime(depart_at, "%Y-%m-%d %H:%M")
        return int(dt.timestamp())
    except Exception:
        return None

def build_gmaps_url(
    origin_text: str, destination_text: str, depart_at: Optional[str],
    *, origin_lat: Optional[float] = None, origin_lon: Optional[float] = None,
       destination_lat: Optional[float] = None, destination_lon: Optional[float] = None
) -> str:
    """
    Google Maps dir/?api=1 の URL を構築（transit, ja/JP）。
    - 座標が与えられた場合は "lat,lng" を優先して origin/destination に使用
    - depart_at は "YYYY-MM-DD HH:MM" or None（解釈失敗は now）
    """
    if not (origin_text or (origin_lat is not None and origin_lon is not None)):
        raise ValueError("origin が未指定です。")
    if not (destination_text or (destination_lat is not None and destination_lon is not None)):
        raise ValueError("destination が未指定です。")

    qs = {
        "api": "1",
        "origin": f"{origin_lat},{origin_lon}" if (origin_lat is not None and origin_lon is not None) else origin_text,
        "destination": f"{destination_lat},{destination_lon}" if (destination_lat is not None and destination_lon is not None) else destination_text,
        "travelmode": "transit",
        "hl": "ja",
        "gl": "JP",
    }

    dep = "now"
    if depart_at:
        try:
            dt = datetime.strptime(depart_at, "%Y-%m-%d %H:%M")  # ローカル時間
            dep = str(int(dt.timestamp()))
        except Exception:
            dep = "now"
    qs["departure_time"] = dep

    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(qs, safe="|,")  # カンマ許可

def save_url_text(
    url: str,
    origin_text: str,
    destination_text: str,
    depart_at: Optional[str],
    tag: Optional[str] = None,
    *,
    dest_label: str = "Destination",
    leg_label: Optional[str] = None,
    epoch_sec: Optional[int] = None,
    origin_coords: Optional[Tuple[float,float]] = None,      # (lat,lng)
    destination_coords: Optional[Tuple[float,float]] = None  # (lat,lng)
) -> Path:
    """URL とメタ情報を /output/*.txt に保存し、保存パスを返す。座標があれば併記。"""
    ensure_output_dir()
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{tag}" if tag else ""
    path = OUTPUT_DIR / f"{now}_route{suffix}.txt"

    depart_line = "now"
    if depart_at:
        depart_line = f"{depart_at} (epoch: {epoch_sec})" if (epoch_sec is not None) else depart_at

    lines = [f"[Generated] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    if leg_label:
        lines.append(f"Leg         : {leg_label}")
    lines += [
        f"Origin      : {origin_text}",
        f"{dest_label:<12}: {destination_text}",
    ]
    if origin_coords:
        lines.append(f"Origin Coord: {origin_coords[0]},{origin_coords[1]}")
    if destination_coords:
        lines.append(f"{dest_label:<12} Coord: {destination_coords[0]},{destination_coords[1]}")
    lines += [
        f"Depart At   : {depart_line}",
        f"URL         : {url}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

# =========================
# Flask アプリ
# =========================
app = Flask(__name__, static_folder=None)

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
def _serve_from_view_roots(filename: str):
    """input.html / style.css / app.js を返す（VIEW_ROOT, PROJECT_ROOT/view を探索）。"""
    candidates = [Path(str(VIEW_ROOT)).resolve(), (Path(str(PROJECT_ROOT)) / "view").resolve()]
    for base in candidates:
        fp = (base / filename).resolve()
        if fp.exists() and fp.is_file():
            return send_from_directory(base.as_posix(), filename)
    abort(404)

@app.get("/")
def route_index():
    return _serve_from_view_roots("input.html")

@app.get("/input.html")
def route_input_html():
    return _serve_from_view_roots("input.html")

@app.get("/style.css")
def route_style_css():
    resp = _serve_from_view_roots("style.css")
    try: resp.headers["Cache-Control"] = "no-store"
    except Exception: pass
    return resp

@app.get("/app.js")
def route_app_js():
    resp = _serve_from_view_roots("app.js")
    try: resp.headers["Cache-Control"] = "no-store"
    except Exception: pass
    return resp

@app.get("/view/<path:filename>")
def route_view_any(filename: str):
    base = (Path(str(PROJECT_ROOT)) / "view").resolve()
    fp = (base / filename).resolve()
    if fp.exists() and fp.is_file():
        return send_from_directory(base.as_posix(), filename)
    abort(404)

# ---------- API: stops ----------
@app.route("/api/stops", methods=["GET", "POST"])
def api_stops():
    ensure_indexers()
    q = request.values.get("q", "")
    limit = int(request.values.get("limit", "50") or "50")
    t0 = time.time()
    hits: List[StopItem] = STOP_INDEXER.search(q, limit=limit) if STOP_INDEXER else []
    elapsed = round(time.time() - t0, 3)

    out_hits: List[Dict[str, Any]] = []
    for it in hits:
        d = asdict(it); d["value"] = it.value; d["snippet"] = it.snippet
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

# ---------- API: 簡易検索 ----------
@app.route("/api/search", methods=["GET", "POST"])
def api_search():
    ensure_indexers()
    q = request.values.get("q", "")
    limit = int(request.values.get("limit", "50") or "50")
    hits, stats = GREP_SEARCH.search(q, limit=limit) if GREP_SEARCH else ([], {})
    return jsonify({"query": q, "hits": hits, "stats": stats})

@app.route("/api/reindex", methods=["GET", "POST"])
def api_reindex():
    global GREP_SEARCH
    GREP_SEARCH = GrepLikeSearch(DATA_ROOT)
    return jsonify({"ok": True})

# ---------- /api/route/save（出発→到着） ----------
@app.post("/api/route/save")
def api_route_save():
    """
    受信JSON:
      { "origin": str, "destination": str, "depart_at": "YYYY-MM-DD HH:MM" or null,
        "origin_lat": float|null, "origin_lon": float|null,
        "destination_lat": float|null, "destination_lon": float|null }
    - URL を生成（座標があれば lat,lng を優先）→ /output/YYYYMMDD_HHMMSS_route.txt に保存
    """
    payload = request.get_json(silent=True) or {}
    origin = (payload.get("origin") or "").strip()
    destination = (payload.get("destination") or "").strip()
    depart_at = (payload.get("depart_at") or None)
    if isinstance(depart_at, str):
        depart_at = depart_at.strip() or None

    o_lat = _num(payload.get("origin_lat")); o_lon = _num(payload.get("origin_lon"))
    d_lat = _num(payload.get("destination_lat")); d_lon = _num(payload.get("destination_lon"))

    if not origin and (o_lat is None or o_lon is None):
        return jsonify({"ok": False, "error": "required: origin or origin_lat/lon"}), 400
    if not destination and (d_lat is None or d_lon is None):
        return jsonify({"ok": False, "error": "required: destination or destination_lat/lon"}), 400

    try:
        url = build_gmaps_url(origin, destination, depart_at,
                              origin_lat=o_lat, origin_lon=o_lon,
                              destination_lat=d_lat, destination_lon=d_lon)
        epoch = _to_epoch_if_possible(depart_at)
        saved_path = str(save_url_text(
            url, origin, destination, depart_at, tag=None,
            dest_label="Destination", leg_label=None, epoch_sec=epoch,
            origin_coords=(o_lat, o_lon) if (o_lat is not None and o_lon is not None) else None,
            destination_coords=(d_lat, d_lon) if (d_lat is not None and d_lon is not None) else None
        ))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"save-failed: {e}"}), 500

    summary = f"{origin or f'({o_lat},{o_lon})'} → {destination or f'({d_lat},{d_lon})'} / depart_at: {depart_at or 'now'}"
    return jsonify({"ok": True, "url": url, "saved_path": saved_path, "summary": summary})

# ---------- /api/route/save_leg1（出発→中間） ----------
@app.post("/api/route/save_leg1")
def api_route_save_leg1():
    """
    受信JSON:
      { "origin": str, "waypoint": str, "depart_at": "YYYY-MM-DD HH:MM" or null,
        "origin_lat": float|null, "origin_lon": float|null,
        "waypoint_lat": float|null, "waypoint_lon": float|null }
    - URL を生成（座標があれば lat,lng を優先）→ /output/YYYYMMDD_HHMMSS_route_leg1.txt に保存
    """
    payload = request.get_json(silent=True) or {}
    origin = (payload.get("origin") or "").strip()
    waypoint = (payload.get("waypoint") or "").strip()
    depart_at = (payload.get("depart_at") or None)
    if isinstance(depart_at, str):
        depart_at = depart_at.strip() or None

    o_lat = _num(payload.get("origin_lat")); o_lon = _num(payload.get("origin_lon"))
    w_lat = _num(payload.get("waypoint_lat")); w_lon = _num(payload.get("waypoint_lon"))

    if not origin and (o_lat is None or o_lon is None):
        return jsonify({"ok": False, "error": "required: origin or origin_lat/lon"}), 400
    if not waypoint and (w_lat is None or w_lon is None):
        return jsonify({"ok": False, "error": "required: waypoint or waypoint_lat/lon"}), 400

    try:
        url = build_gmaps_url(origin, waypoint, depart_at,
                              origin_lat=o_lat, origin_lon=o_lon,
                              destination_lat=w_lat, destination_lon=w_lon)
        epoch = _to_epoch_if_possible(depart_at)
        saved_path = str(save_url_text(
            url, origin, waypoint, depart_at, tag="leg1",
            dest_label="Waypoint", leg_label="1 (Origin → Waypoint)", epoch_sec=epoch,
            origin_coords=(o_lat, o_lon) if (o_lat is not None and o_lon is not None) else None,
            destination_coords=(w_lat, w_lon) if (w_lat is not None and w_lon is not None) else None
        ))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"save-failed: {e}"}), 500

    summary = f"{origin or f'({o_lat},{o_lon})'} → {waypoint or f'({w_lat},{w_lon})'} (Leg1) / depart_at: {depart_at or 'now'}"
    return jsonify({"ok": True, "url": url, "saved_path": saved_path, "summary": summary})

# =========================
# 自動ブラウザ起動（省略…前回同様）
# =========================
def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def _launch_browser_when_ready(url: str, probe_host: str, port: int, delay: float = 0.8, timeout: float = 15.0) -> None:
    try:
        time.sleep(max(0.0, delay))
        start = time.time()
        probe = "127.0.0.1" if probe_host in ("0.0.0.0", "::") else probe_host
        while time.time() - start < timeout:
            if _is_port_open(probe, port):
                webbrowser.open(url); return
            time.sleep(0.25)
    except Exception:
        pass

def _print_banner(host: str, port: int):
    print("====================================")
    print(" Local Web Server  (stops/search/static + route save)")
    print("====================================")
    print(f" Project : {PROJECT_ROOT}")
    print(f" Serve   : {VIEW_ROOT}  ( / → input.html )")
    print(f" Roots   : [{DATA_ROOT}]")
    print(" API     :")
    print("   GET/POST /api/stops[?q=&limit=]")
    print("   GET/POST /api/stops/reindex")
    print("   GET/POST /api/search")
    print("   GET/POST /api/reindex")
    print("   POST     /api/route/save        (出発→到着 保存: 座標優先)")
    print("   POST     /api/route/save_leg1   (出発→中間 保存: 座標優先)")
    print(" Stop    : Ctrl + C")
    print("------------------------------------")
    print(f" Host/Port : {host}:{port}")
    print(f" Open URL  : http://127.0.0.1:{port}/")
    print("====================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Web Server for station/bus stop search")
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument("--no-open", action="store_true", help="do NOT auto-open default browser")
    parser.add_argument("--open-delay", type=float, default=0.8, help="seconds to wait before probing and opening browser")
    args = parser.parse_args()

    host = args.host; port = args.port
    auto_open = not args.no_open; open_delay = max(0.0, args.open_delay)

    _print_banner(host, port)

    if auto_open:
      url = f"http://127.0.0.1:{port}/"
      th = threading.Thread(target=_launch_browser_when_ready, args=(url, host, port, open_delay), daemon=True)
      th.start()

    from werkzeug.serving import WSGIRequestHandler
    try: WSGIRequestHandler.protocol_version = "HTTP/1.1"
    except Exception: pass

    app.run(host=host, port=port, debug=False)
