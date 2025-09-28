
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
prep_transport_poi.py (Enhanced)

変更点
------
- --n05 / --p11 に「ZIP以外」も指定可能（ディレクトリ / 単一ファイル / グロブパターン）
  例: --n05 "C:/data/N05-20_GML" / --p11 "C:/data/P11-22_GML/**/*.gml" / --n05 path/to/file.gml
- フラグの複数回指定に対応（--n05 を何度でも）

入力として認識される形式
------------------------
- ZIP (.zip) … 自動で展開し GML を収集
- ディレクトリ … 再帰的に .gml / .gpkg / .geojson / .shp を収集
- ファイル … 上記拡張子の単一ファイルを収集
- グロブ … パターンに一致するファイルを収集（例: "**/*.gml"）

出力・API は従来通り：
  out/stations.parquet, out/busstops.parquet, out/poi.parquet, out/names_index.json
  from prep_transport_poi import POIIndex
"""

from __future__ import annotations
import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
import numpy as np

# ---- Optional / Heavy deps (loaded lazily) ----
def _lazy_import():
    try:
        import geopandas as gpd  # type: ignore
    except Exception:
        gpd = None
    try:
        import pyogrio  # noqa: F401  # type: ignore
    except Exception:
        pass
    try:
        from scipy.spatial import cKDTree as KDTree  # type: ignore
    except Exception:
        KDTree = None
    return gpd

# ========== Utilities ==========

SUFFIXES_FOR_KEY = ["駅", "停留所", "バス停"]

_ZEN_BRACKETS = "（）［］｛｝【】〈〉《》「」『』〔〕"

def normalize_text(s: str) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = re.sub(r"[\s\u3000]+", " ", s)
    s = s.translate(str.maketrans({c: "" for c in _ZEN_BRACKETS}))
    s = s.translate(str.maketrans({c: "" for c in "()[]{}<>\"'"}))
    return s

def build_key(name: str) -> str:
    base = normalize_text(name)
    for suf in SUFFIXES_FOR_KEY:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    return base.lower()

def guess_name_column(columns: Iterable[str]) -> Optional[str]:
    candidates = [
        "駅名", "名称", "代表点名", "代表名称", "停留所名", "バス停名",
        "n02_005", "n05_001", "p11_001",
        "name", "station", "title", "label",
    ]
    for cand in candidates:
        for c in columns:
            if c.lower() == cand.lower():
                return c
    for c in columns:
        if ("名" in c) or ("name" in c.lower()):
            return c
    return None

def guess_operator_column(columns: Iterable[str]) -> Optional[str]:
    candidates = ["事業者名", "運営会社名", "運行会社", "運行事業者", "operator", "company", "operator_name", "n02_003", "n02_004"]
    for cand in candidates:
        for c in columns:
            if c.lower() == cand.lower():
                return c
    return None

def guess_line_column(columns: Iterable[str]) -> Optional[str]:
    candidates = ["路線名", "line", "line_name", "n02_002"]
    for cand in candidates:
        for c in columns:
            if c.lower() == cand.lower():
                return c
    return None

# ========== IO helpers ==========

def extract_gml_from_zip(zip_path: Path, out_dir: Path) -> List[Path]:
    import zipfile
    out_dir.mkdir(parents=True, exist_ok=True)
    gmls: List[Path] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for m in zf.infolist():
            if m.filename.lower().endswith(".gml"):
                zf.extract(m, out_dir)
                gmls.append(out_dir / m.filename)
    return gmls

SUPPORTED_VECTORS = (".gml", ".gpkg", ".geojson", ".json", ".shp")

def collect_inputs(spec: str, scratch_dir: Path) -> List[Path]:
    """
    Expand a single spec into a list of vector-file paths.
    spec:
      - zip file      -> extract and return GMLs
      - directory     -> recurse and find supported files
      - file          -> if supported, return [file]
      - glob pattern  -> expand and filter supported files
    """
    paths: List[Path] = []
    p = Path(spec)

    # glob?
    if any(ch in spec for ch in "*?"):
        for m in Path(".").glob(spec):
            if m.is_file() and m.suffix.lower() in SUPPORTED_VECTORS:
                paths.append(m.resolve())
        return paths

    # file?
    if p.is_file():
        if p.suffix.lower() == ".zip":
            subdir = scratch_dir / p.stem
            return extract_gml_from_zip(p, subdir)
        if p.suffix.lower() in SUPPORTED_VECTORS:
            return [p.resolve()]
        raise SystemExit(f"未対応の拡張子です: {p.suffix} ({p})")

    # directory?
    if p.is_dir():
        for m in p.rglob("*"):
            if m.is_file() and m.suffix.lower() in SUPPORTED_VECTORS:
                paths.append(m.resolve())
        return paths

    raise SystemExit(f"指定パスが見つかりません: {spec}")

def read_any_vector(path: Path):
    gpd = _lazy_import()
    if gpd is None:
        raise RuntimeError("geopandas が見つかりません。`pip install -r requirements.txt` を実行してください。")
    try:
        gdf = gpd.read_file(path)
    except Exception as e:
        raise RuntimeError(f"ベクターデータ読込に失敗: {path}\n{e}")
    return gdf

def to_wgs84(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(4326, allow_override=True)
    epsg = None
    try:
        epsg = gdf.crs.to_epsg()
    except Exception:
        pass
    if epsg != 4326:
        gdf = gdf.to_crs(4326)
    return gdf

def canonicalize(gdf, kind: str) -> pd.DataFrame:
    name_col = guess_name_column(gdf.columns)
    if name_col is None:
        raise RuntimeError(f"{kind}: 名前列を特定できませんでした。列候補の例: {list(gdf.columns)[:8]} ...")

    op_col = guess_operator_column(gdf.columns)
    line_col = guess_line_column(gdf.columns)

    gdf = to_wgs84(gdf)

    id_series = None
    for cid in ["station_id", "busstop_id", "id", "ID", "NID", "node_id", "link_id", "gml_id"]:
        if cid in gdf.columns:
            id_series = gdf[cid].astype(str)
            break
    if id_series is None:
        id_series = gdf.index.astype(str)

    # geometry -> lat/lon（ポイント前提。ポイント以外は centroid にする）
    geom = gdf.geometry
    try:
        y = geom.y
        x = geom.x
    except Exception:
        geom = geom.centroid
        y = geom.y
        x = geom.x

    out = pd.DataFrame({
        "id": id_series,
        "kind": kind,
        "name": gdf[name_col].astype(str).fillna(""),
        "operator": gdf[op_col].astype(str) if (op_col and op_col in gdf.columns) else "",
        "line": gdf[line_col].astype(str) if (line_col and line_col in gdf.columns) else "",
        "lat": y,
        "lon": x,
    })
    out["name_key"] = out["name"].map(build_key)
    out = out.drop_duplicates(subset=["kind", "name_key", "lat", "lon"])
    return out

# ========== Index helper ==========

class POIIndex:
    def __init__(self, parquet_path: str | Path, names_index_json: str | Path):
        self.parquet_path = Path(parquet_path)
        self.names_index_json = Path(names_index_json)
        self.df = pd.read_parquet(self.parquet_path)
        self.df["_key"] = self.df["name"].map(build_key)
        with open(self.names_index_json, "r", encoding="utf-8") as f:
            self.names_index: Dict[str, List[int]] = json.load(f)
        try:
            from scipy.spatial import cKDTree as KDTree
            self._kdtree = KDTree(self.df[["lat", "lon"]].to_numpy(dtype=float))
        except Exception:
            self._kdtree = None

    def find_exact(self, name: str, kind: Optional[str] = None) -> pd.DataFrame:
        key = build_key(name)
        idxs = self.names_index.get(key, [])
        res = self.df.iloc[idxs]
        if kind:
            res = res[res["kind"] == kind]
        return res.copy()

    def find_substring(self, query: str, kind: Optional[str] = None) -> pd.DataFrame:
        q = normalize_text(query).lower()
        res = self.df[self.df["_key"].str.contains(q, na=False)]
        if kind:
            res = res[res["kind"] == kind]
        return res.copy()

    def find_fuzzy(self, query: str, limit: int = 10, kind: Optional[str] = None) -> pd.DataFrame:
        try:
            from rapidfuzz import process, fuzz
        except Exception as e:
            raise RuntimeError("rapidfuzz が必要です: pip install rapidfuzz") from e
        key = build_key(query)
        choices = list(self.names_index.keys())
        matches = process.extract(key, choices, scorer=fuzz.WRatio, limit=limit)
        keys = [k for k, score, _ in matches if score >= 70]
        idxs = [i for k in keys for i in self.names_index.get(k, [])]
        res = self.df.iloc[idxs].copy()
        if kind:
            res = res[res["kind"] == kind]
        return res

    def nearest(self, lat: float, lon: float, k: int = 5, kind: Optional[str] = None) -> pd.DataFrame:
        if getattr(self, "_kdtree", None) is None:
            raise RuntimeError("最近傍検索には scipy が必要です: pip install scipy")
        import numpy as np
        dists, idxs = self._kdtree.query(np.array([[lat, lon]], dtype=float), k=k)
        if np.ndim(idxs) == 0:
            idxs = [int(idxs)]
        res = self.df.iloc[idxs].copy()
        res["approx_m"] = (dists * 111_000).astype(int) if np.ndim(dists) else int(dists * 111_000)
        if kind:
            res = res[res["kind"] == kind]
        return res

# ========== Build pipeline ==========

@dataclass
class BuildConfig:
    n05_specs: Sequence[str]
    p11_specs: Sequence[str]
    out_dir: Path

def build(cfg: BuildConfig) -> None:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    scratch = cfg.out_dir / "_extracted"
    scratch.mkdir(exist_ok=True)

    frames: List[pd.DataFrame] = []

    for spec in cfg.n05_specs:
        for path in collect_inputs(spec, scratch_dir=scratch / "N05"):
            gdf = read_any_vector(path)
            df = canonicalize(gdf, kind="station")
            frames.append(df)

    for spec in cfg.p11_specs:
        for path in collect_inputs(spec, scratch_dir=scratch / "P11"):
            gdf = read_any_vector(path)
            df = canonicalize(gdf, kind="bus_stop")
            frames.append(df)

    if not frames:
        raise SystemExit("入力がありません。--n05 / --p11 に ZIP/ディレクトリ/ファイル/グロブ を指定してください。")

    all_df = pd.concat(frames, ignore_index=True)

    stations = all_df[all_df["kind"] == "station"]
    busstops = all_df[all_df["kind"] == "bus_stop"]
    if not stations.empty:
        stations.to_parquet(cfg.out_dir / "stations.parquet", index=False)
    if not busstops.empty:
        busstops.to_parquet(cfg.out_dir / "busstops.parquet", index=False)

    all_df.to_parquet(cfg.out_dir / "poi.parquet", index=False)

    name_to_idxs: Dict[str, List[int]] = {}
    for i, k in enumerate(all_df["name_key"].tolist()):
        name_to_idxs.setdefault(k, []).append(i)
    with open(cfg.out_dir / "names_index.json", "w", encoding="utf-8") as f:
        json.dump(name_to_idxs, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[OK] Wrote: {cfg.out_dir}/stations.parquet (exists={not stations.empty})")
    print(f"[OK] Wrote: {cfg.out_dir}/busstops.parquet (exists={not busstops.empty})")
    print(f"[OK] Wrote: {cfg.out_dir}/poi.parquet")
    print(f"[OK] Wrote: {cfg.out_dir}/names_index.json")

# ========== CLI ==========

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="駅(N05)/バス停(P11)のGML等から検索しやすいインデックスを構築")
    p.add_argument("--n05", action="append", type=str, default=[], help="N05 の ZIP/ディレクトリ/ファイル/グロブ（複数指定可）")
    p.add_argument("--p11", action="append", type=str, default=[], help="P11 の ZIP/ディレクトリ/ファイル/グロブ（複数指定可）")
    p.add_argument("-o", "--out", type=Path, required=True, help="出力ディレクトリ")
    args = p.parse_args(argv)

    cfg = BuildConfig(n05_specs=args.n05, p11_specs=args.p11, out_dir=args.out)
    build(cfg)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
