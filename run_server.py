# -*- coding: utf-8 -*-
"""
run_server.py
目的:
  - view/ 配下の静的ファイル（input.html, app.js, style.css など）を配信するローカルWebサーバ。
  - /api/search で「ローカルデータ検索」を提供する。検索対象は以下の優先順位で決定:
        1) 環境変数 DATA_ROOT のパス（指定があれば最優先）
        2) <プロジェクト>/data  （これが既定・推奨）
        3) /data                （最後のフォールバック）
  - /api/reindex でディレクトリインデックスを再構築。
  - GET/POST/OPTIONS を実装（501対策）。CORSヘッダー付与。

できること（概要）:
  - http://127.0.0.1:<port>/ で view/input.html を返す（/ → input.html）。
  - GET /api/search?q=キーワード[&mode=dir|zip|both][&scope=filename|content|both][&limit=200][&force=true]
  - POST /api/search （JSON もしくは x-www-form-urlencoded で上記パラメータ）
  - GET/POST /api/reindex で <検索ルート> のファイル一覧キャッシュを作り直す。
  - いずれの検索モードでも、検索対象はあくまで「データルート配下」に限定される。

依存:
  - Python 3.8+（標準ライブラリのみ）

起動例:
  > python run_server.py
  （初期起動時に <プロジェクト>/data が無ければ空フォルダを作成）
"""

from __future__ import annotations

import os
import sys
import io
import json
import time
import glob
import contextlib
import zipfile
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from typing import Iterable, Dict, Any, List
from urllib.parse import urlparse, parse_qs, unquote

# =========================
# 設定値・定数
# =========================

HOST: str = "127.0.0.1"                         # バインド先
PORT_CANDIDATES: List[int] = list(range(8000, 8011))  # 空きを順に試すポート

# パス周り
BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))  # プロジェクトルート
VIEW_DIR: str = os.path.join(BASE_DIR, "view")              # 静的配信ルート
PROJ_DATA_ROOT: str = os.path.join(BASE_DIR, "data")        # 既定: <プロジェクト>/data
ENV_DATA_ROOT: str = os.environ.get("DATA_ROOT", "")        # ユーザーが上書きしたい時に使用
ABS_DATA_ROOT: str = "/data"                                # 最後のフォールバック

# 検索対象拡張子（テキスト系）
TEXT_EXT_WHITELIST: set[str] = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".gml", ".html", ".htm", ".md", ".geojson"
}

# 制限（負荷＆安全弁）
MAX_BYTES_PER_FILE: int = 2 * 1024 * 1024   # 1ファイルの読み取り上限（2MB）
MAX_HITS_RETURNED: int = 200                # 返却件数上限
MAX_FILES_SCANNED: int = 200000             # 総スキャン数の安全上限
INDEX_REFRESH_SEC: int = 60                 # ディレクトリ一覧キャッシュの再構築間隔（秒）

# 文字コード候補（順に試す）
DECODE_CANDIDATES: list[str] = ["utf-8", "cp932", "shift_jis", "euc-jp", "latin1"]

# =========================
# 検索ルートの候補と決定
# =========================

def _candidate_data_roots() -> list[str]:
    """
    データルート候補を優先順位順で返す。
      1) DATA_ROOT（環境変数）
      2) <プロジェクト>/data
      3) /data
    """
    cands: list[str] = []
    if ENV_DATA_ROOT:
        cands.append(ENV_DATA_ROOT)
    cands.append(PROJ_DATA_ROOT)
    cands.append(ABS_DATA_ROOT)

    # 正規化＆重複除去
    out: list[str] = []
    seen: set[str] = set()
    for p in cands:
        if not p:
            continue
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out

def _zip_root_candidates() -> list[str]:
    """ZIP探索のためのルート候補（データルートと同一）"""
    return _candidate_data_roots()

# =========================
# グローバルキャッシュ
# =========================

_DIR_FILE_LIST: list[str] = []     # 走査対象ファイル一覧（テキスト系のみ）
_DIR_FILE_LIST_STAMP: float = 0.0  # 最終更新時刻
_DIR_ROOTS_ACTUAL: list[str] = []  # 実在が確認できたルート

# =========================
# 低レベルユーティリティ
# =========================

def _try_decode(b: bytes) -> str:
    """バイト列を各種エンコーディングでデコード。失敗時は latin1 ignore。"""
    for enc in DECODE_CANDIDATES:
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("latin1", errors="ignore")

def _is_text_ext(path: str) -> bool:
    """テキストとして扱う拡張子か判定。"""
    _, ext = os.path.splitext(path)
    return ext.lower() in TEXT_EXT_WHITELIST

def _normalize_relpath(abs_path: str) -> str:
    """
    表示用に「データルートからの相対パス」っぽく見せる。
    複数ルートのどれにも一致しなければ絶対パスを / 区切りで返す。
    """
    ap = os.path.abspath(abs_path)
    for root in _DIR_ROOTS_ACTUAL:
        root_abs = os.path.abspath(root)
        if ap == root_abs or ap.startswith(root_abs + os.sep):
            return os.path.relpath(ap, start=root_abs).replace("\\", "/")
    return ap.replace("\\", "/")

# =========================
# ZIP検索（任意。/data系のみ）
# =========================

def _discover_zip_paths() -> list[str]:
    """データルート候補配下の *.zip を列挙。"""
    paths: list[str] = []
    seen: set[str] = set()
    for d in _zip_root_candidates():
        if not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, "*.zip"))):
            if os.path.isfile(p) and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths

def _search_in_zip(zip_path: str, query_lower: str, scope: str, limit_hits: int) -> Iterable[dict[str, Any]]:
    """
    1つのZIPアーカイブ内で検索。
      - scope: "filename" | "content" | "both"
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entry_name = info.filename

                # ファイル名一致
                if scope in ("filename", "both") and (query_lower in entry_name.lower()):
                    yield {
                        "source": "zip",
                        "zip": os.path.basename(zip_path),
                        "path": entry_name,
                        "relpath": entry_name,
                        "reason": "name",
                        "snippet": "",
                        "value": entry_name,
                    }
                    continue

                # 内容一致（テキスト拡張子のみ読み取り）
                if scope in ("content", "both") and _is_text_ext(entry_name):
                    try:
                        with zf.open(info, "r") as fp:
                            b = fp.read(MAX_BYTES_PER_FILE)
                    except Exception:
                        continue
                    text = _try_decode(b)
                    idx = text.lower().find(query_lower)
                    if idx >= 0:
                        start = max(0, idx - 40)
                        end = min(len(text), idx + 40)
                        snippet = text[start:end].replace("\r", "").replace("\n", " ")
                        yield {
                            "source": "zip",
                            "zip": os.path.basename(zip_path),
                            "path": entry_name,
                            "relpath": entry_name,
                            "reason": "content",
                            "snippet": snippet,
                            "value": text[idx: idx + len(query_lower)] or entry_name,
                        }
    except Exception as e:
        sys.stderr.write(f"[SEARCH][ZIP] open error: {zip_path}: {e}\n")

# =========================
# ディレクトリ検索
# =========================

def _refresh_dir_roots() -> list[str]:
    """存在するデータルートのみ抽出して保持。"""
    global _DIR_ROOTS_ACTUAL
    roots: list[str] = []
    for d in _candidate_data_roots():
        if os.path.isdir(d):
            roots.append(d)
    _DIR_ROOTS_ACTUAL = roots
    return roots

def _rebuild_dir_file_list() -> None:
    """
    データルート配下の「テキスト拡張子のファイル」を一覧化してキャッシュに格納。
    """
    global _DIR_FILE_LIST, _DIR_FILE_LIST_STAMP

    roots = _refresh_dir_roots()
    files: list[str] = []
    scanned = 0

    for root in roots:
        for cur, _dirs, names in os.walk(root):
            if scanned >= MAX_FILES_SCANNED:
                break
            for nm in names:
                if scanned >= MAX_FILES_SCANNED:
                    break
                path = os.path.join(cur, nm)
                scanned += 1
                if _is_text_ext(path):
                    files.append(path)

    _DIR_FILE_LIST = files
    _DIR_FILE_LIST_STAMP = time.time()

    if not roots:
        # 候補を列挙した上で警告（ユーザーの状況把握を助ける）
        sys.stderr.write("[INDEX] WARNING: データフォルダが見つかりません。候補:\n")
        sys.stderr.write(f"  - DATA_ROOT={ENV_DATA_ROOT or '(未設定)'}\n")
        sys.stderr.write(f"  - {PROJ_DATA_ROOT}\n")
        sys.stderr.write(f"  - {ABS_DATA_ROOT}\n")
    else:
        sys.stderr.write(f"[INDEX] roots={roots}\n")
    sys.stderr.write(f"[INDEX] files={len(files)} stamp={_DIR_FILE_LIST_STAMP}\n")

def _ensure_dir_file_list_recent(force: bool = False) -> None:
    """キャッシュが古い/空なら再構築。force=True で強制再構築。"""
    if force or (time.time() - _DIR_FILE_LIST_STAMP > INDEX_REFRESH_SEC) or not _DIR_FILE_LIST:
        _rebuild_dir_file_list()

def _search_in_file(file_path: str, query_lower: str, scope: str) -> Iterable[dict[str, Any]]:
    """
    単一ファイルでの検索。
      - scope: "filename" | "content" | "both"
    """
    rel = _normalize_relpath(file_path)

    # ファイル名一致
    if scope in ("filename", "both"):
        name = os.path.basename(file_path)
        if query_lower in name.lower() or query_lower in rel.lower():
            yield {
                "source": "dir",
                "zip": "",
                "path": file_path,
                "relpath": rel,
                "reason": "name",
                "snippet": "",
                "value": name,
            }
            return

    # 内容一致（先頭 MAX_BYTES_PER_FILE のみ読み取り）
    if scope in ("content", "both"):
        try:
            with open(file_path, "rb") as f:
                b = f.read(MAX_BYTES_PER_FILE)
        except Exception:
            return
        text = _try_decode(b)
        idx = text.lower().find(query_lower)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(text), idx + 40)
            snippet = text[start:end].replace("\r", "").replace("\n", " ")
            value = text[idx: idx + len(query_lower)] or os.path.basename(file_path)
            yield {
                "source": "dir",
                "zip": "",
                "path": file_path,
                "relpath": rel,
                "reason": "content",
                "snippet": snippet,
                "value": value,
            }

# =========================
# 検索本体
# =========================

def perform_search(query: str,
                   mode: str = "dir",
                   scope: str = "both",
                   max_hits: int = MAX_HITS_RETURNED,
                   force_reindex: bool = False) -> dict[str, Any]:
    """
    総合検索関数。
      - query: 検索語（大小無視）
      - mode : "dir"(既定) | "zip" | "both"  ※いずれも「データルート配下」に限定
      - scope: "filename" | "content" | "both"
      - max_hits: 返却上限
      - force_reindex: ディレクトリファイル一覧の強制再構築
    """
    t0 = time.time()
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty_query", "message": "検索語が空です。"}

    ql = q.lower()
    hits: list[dict[str, Any]] = []
    scanned_zip_count = 0

    # 1) ディレクトリ検索
    if mode in ("dir", "both"):
        _ensure_dir_file_list_recent(force=force_reindex)
        for fp in _DIR_FILE_LIST:
            for h in _search_in_file(fp, ql, scope=scope):
                hits.append(h)
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break

    # 2) ZIP検索（任意）
    if len(hits) < max_hits and mode in ("zip", "both"):
        for zp in _discover_zip_paths():
            for h in _search_in_zip(zp, ql, scope=scope, limit_hits=max_hits):
                hits.append(h)
                if len(hits) >= max_hits:
                    break
            scanned_zip_count += 1
            if len(hits) >= max_hits:
                break

    dt = time.time() - t0
    return {
        "ok": True,
        "query": q,
        "hits": hits,
        "stats": {
            "mode": mode,
            "scope": scope,
            "dir_files_considered": len(_DIR_FILE_LIST) if mode in ("dir", "both") else 0,
            "zip_files_considered": scanned_zip_count if mode in ("zip", "both") else 0,
            "elapsed_sec": round(dt, 3),
            "truncated": len(hits) >= max_hits,
            "roots": _DIR_ROOTS_ACTUAL,
        }
    }

# =========================
# HTTP ハンドラ
# =========================

class IndexFileHandler(SimpleHTTPRequestHandler):
    """
    役割:
      - /api/search   : 検索API（GET/POST）
      - /api/reindex  : インデックス再構築（GET/POST）
      - /api/ping     : 生存確認
      - /, /index.html: input.html にルーティング
      - それ以外      : view/ 配下の静的配信

    備考:
      - CORS対応（同一オリジンでも付けておくとトラブル回避しやすい）
      - do_OPTIONS も実装し、プリフライト要求にも 204 で応答（501回避）
    """

    # ---- GET ---------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/search":
            return self._handle_api_search(parsed)
        if parsed.path == "/api/reindex":
            _rebuild_dir_file_list()
            return self._send_json({"ok": True, "reindexed": True, "files": len(_DIR_FILE_LIST), "roots": _DIR_ROOTS_ACTUAL})
        if parsed.path == "/api/ping":
            return self._send_json({"ok": True, "pong": True})

        if parsed.path in ("/", "/index.html"):
            self.path = "/input.html"
        return super().do_GET()

    # ---- POST --------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/search":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            ctype = (self.headers.get("Content-Type") or "").lower()

            # 既定値
            q = ""
            mode = "dir"
            scope = "both"
            limit = MAX_HITS_RETURNED
            force = False

            try:
                if "application/json" in ctype:
                    obj = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
                    q = str(obj.get("q") or "").strip()
                    mode = (obj.get("mode") or "dir").lower()
                    scope = (obj.get("scope") or "both").lower()
                    limit = max(1, min(int(obj.get("limit") or MAX_HITS_RETURNED), MAX_HITS_RETURNED))
                    force = bool(obj.get("force") or False)
                else:
                    # x-www-form-urlencoded
                    qs = parse_qs(raw.decode("utf-8", errors="ignore"))
                    q = (qs.get("q", [""])[0] or "").strip()
                    mode = (qs.get("mode", ["dir"])[0] or "dir").lower()
                    scope = (qs.get("scope", ["both"])[0] or "both").lower()
                    limit = max(1, min(int(qs.get("limit", [str(MAX_HITS_RETURNED)])[0]), MAX_HITS_RETURNED))
                    force = (qs.get("force", ["false"])[0] or "false").lower() in ("1", "true", "yes")
            except Exception as e:
                return self._send_json({"ok": False, "error": "bad_request", "message": f"parse error: {e}"}, status=400)

            result = perform_search(q, mode=mode, scope=scope, max_hits=limit, force_reindex=force)
            return self._send_json(result)

        if parsed.path == "/api/reindex":
            _rebuild_dir_file_list()
            return self._send_json({"ok": True, "reindexed": True, "files": len(_DIR_FILE_LIST), "roots": _DIR_ROOTS_ACTUAL})

        return self._send_json({"ok": False, "error": "not_found"}, status=404)

    # ---- OPTIONS（CORS プリフライト） -------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.end_headers()

    # ---- 内部: /api/search(GET) ------------------------------------
    def _handle_api_search(self, parsed):
        qs = parse_qs(parsed.query)
        q = unquote((qs.get("q", [""])[0] or "").strip())
        mode = (qs.get("mode", ["dir"])[0] or "dir").lower()
        scope = (qs.get("scope", ["both"])[0] or "both").lower()
        limit_s = (qs.get("limit", [str(MAX_HITS_RETURNED)])[0] or str(MAX_HITS_RETURNED))
        try:
            limit = max(1, min(int(limit_s), MAX_HITS_RETURNED))
        except Exception:
            limit = MAX_HITS_RETURNED
        force = (qs.get("force", ["false"])[0] or "false").lower() in ("1", "true", "yes")

        result = perform_search(q, mode=mode, scope=scope, max_hits=limit, force_reindex=force)
        return self._send_json(result)

    # ---- 内部: JSON 応答ユーティリティ -----------------------------
    def _send_json(self, data: Dict[str, Any], status: int = 200):
        try:
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        except Exception as e:
            status = 500
            body = json.dumps({"ok": False, "error": "json_dump_failed", "message": str(e)}).encode("utf-8")

        self.send_response(status)
        # CORS（同一オリジンでも付与しておく）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ログ出力（SimpleHTTPRequestHandlerの書式を踏襲）
    def log_message(self, fmt, *args):
        sys.stderr.write("[HTTP] " + fmt % args + "\n")

# =========================
# サーバ起動系
# =========================

def _prepare_on_boot():
    """起動前準備: view/ 存在チェック、data/ 作成、インデックス構築"""
    if not os.path.isdir(VIEW_DIR):
        print(f"[ERROR] view ディレクトリが見つかりません: {VIEW_DIR}")
        sys.exit(1)

    # 既定の <プロジェクト>/data が無ければ空で作成（ユーザーに優しい挙動）
    if not os.path.isdir(PROJ_DATA_ROOT):
        with contextlib.suppress(Exception):
            os.makedirs(PROJ_DATA_ROOT, exist_ok=True)

    _rebuild_dir_file_list()  # 初期インデックス

def _find_free_server():
    """空きポートを見つけて HTTP サーバを返す。"""
    last_error = None
    for port in PORT_CANDIDATES:
        try:
            handler = partial(IndexFileHandler, directory=VIEW_DIR)
            httpd = ThreadingHTTPServer((HOST, port), handler)
            return httpd, port
        except OSError as e:
            last_error = e
            continue
    raise OSError(f"Failed to bind any of ports {PORT_CANDIDATES}: {last_error!r}")

def main():
    _prepare_on_boot()

    httpd, port = _find_free_server()
    url = f"http://{HOST}:{port}/"

    print("====================================")
    print(" Local Web Server")
    print("====================================")
    print(f" Project : {BASE_DIR}")
    print(f" Serve   : {VIEW_DIR}  ( / → input.html )")
    print(f" Roots   : {_DIR_ROOTS_ACTUAL or ['(not found)']}")
    print(" API     : GET /api/search?q=...&mode=dir|zip|both&scope=filename|content|both&limit=N")
    print("           POST /api/search (JSON or form: q,mode,scope,limit,force)")
    print("           GET/POST /api/reindex")
    print(" Tips    : 既定では <プロジェクト>/data を検索します。DATA_ROOT で上書き可能。")
    print(" Stop    : Ctrl + C")
    print("====================================")

    with contextlib.suppress(Exception):
        webbrowser.open(url, new=2)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")
    finally:
        with contextlib.suppress(Exception):
            httpd.server_close()

if __name__ == "__main__":
    main()
