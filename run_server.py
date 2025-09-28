# run_server.py
# 目的:
#   - view/ 配下を静的配信し、/ で input.html を返すローカルWebサーバ。
#   - /api/search で「/data 配下のみ」を再帰検索（大量検索向けにディレクトリ検索が既定）。
#   - /api/reindex で /data のファイル一覧キャッシュを再構築。
#   - 必要なら ZIP 検索も可能だが、/data 配下のZIPのみ対象（mode=zip|both）。
#
# 変更点（今回の要望対応）:
#   - 検索範囲を /data 配下のみにハードリミット。
#   - ZIP検索の探索ディレクトリも /data のみに限定。
#
# 使い方:
#   python run_server.py
#   → http://127.0.0.1:8000/ を開く
#   → 入力欄で検索（既定: mode=dir）。/data 以下が対象。
#
# API:
#   GET /api/search?q=KEYWORD[&mode=dir|zip|both][&scope=filename|content|both][&limit=200]
#   GET /api/reindex
#
# 注意:
#   - 想定はローカル用途。/data が存在しない場合はインデックス件数0になり、検索結果は空です。
#   - /data に解凍済みの .xml / .gml / .json / .csv / .txt などを配置してください。
#   - ZIPも使う場合は /data 配下に ZIP を置き、mode=both|zip で検索可能です。

from __future__ import annotations

import os
import sys
import io
import json
import time
import glob
import contextlib
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import webbrowser
import zipfile
from typing import Iterable, List, Dict, Any

# =========================
# 基本パラメータ（要件に合わせ固定）
# =========================

HOST: str = "127.0.0.1"                            # サーバホスト
PORT_CANDIDATES: list[int] = list(range(8000, 8011))  # 空きポートを探索

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))  # プロジェクトルート
VIEW_DIR: str = os.path.join(BASE_DIR, "view")              # 静的ファイル配置

# ★ 検索ルートを /data のみに限定（存在しなければ後述のインデックスは空）
DIR_SEARCH_ROOTS_CANDIDATES: list[str] = [
    "/data",
]

# ★ ZIP検索も /data のみ
ZIP_DIR_CANDIDATES: list[str] = [
    "/data",
]
# 参考: よく使うZIP名が /data にある場合に優先チェック（無ければスキップ）
PREFERRED_ZIPS: list[str] = [
    "/data/N05-20_GML.zip",
    "/data/P11-22_GML.zip",
]

# テキスト拡張子（対象ファイル種別のホワイトリスト）
TEXT_EXT_WHITELIST: set[str] = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".gml", ".html", ".htm", ".md"
}

# 内容読み取り上限（過負荷防止）
MAX_BYTES_PER_FILE: int = 2 * 1024 * 1024  # 2MB
# 返却件数上限
MAX_HITS_RETURNED: int = 200
# 総スキャン安全弁（極端な巨大ツリー対策）
MAX_FILES_SCANNED: int = 200000

# デコード候補（上から順に試行）
DECODE_CANDIDATES: list[str] = ["utf-8", "cp932", "shift_jis", "euc-jp", "latin1"]

# ディレクトリインデックスの自動更新間隔（秒）
INDEX_REFRESH_SEC: int = 60

# ========== グローバルキャッシュ ==========
_DIR_FILE_LIST: list[str] = []     # /data 配下の対象ファイル一覧
_DIR_FILE_LIST_STAMP: float = 0.0  # 最終更新時刻
_DIR_ROOTS_ACTUAL: list[str] = []  # 実在した検索ルート（相対パス計算用）


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
    """拡張子がテキスト許容か判定。"""
    _, ext = os.path.splitext(path)
    return ext.lower() in TEXT_EXT_WHITELIST


def _normalize_relpath(abs_path: str) -> str:
    """
    表示用の相対パスを、実在する検索ルートからの相対に正規化。
    どれにも当てはまらない場合は /data からの相対（存在しない場合はそのまま）。
    """
    ap = os.path.abspath(abs_path)
    for root in _DIR_ROOTS_ACTUAL:
        root_abs = os.path.abspath(root)
        if ap.startswith(root_abs + os.sep) or ap == root_abs:
            return os.path.relpath(ap, start=root_abs).replace("\\", "/")
    try:
        return os.path.relpath(ap, start=os.path.abspath("/data")).replace("\\", "/")
    except Exception:
        return abs_path.replace("\\", "/")


# =========================
# ZIP 検索ユーティリティ（/dataのみ）
# =========================

def _discover_zip_paths() -> list[str]:
    """/data 配下のZIPを列挙（優先ファイル → ディレクトリの順）。"""
    seen: set[str] = set()
    paths: list[str] = []

    for p in PREFERRED_ZIPS:
        if os.path.isfile(p) and p not in seen:
            seen.add(p)
            paths.append(p)

    for d in ZIP_DIR_CANDIDATES:
        if not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, "*.zip"))):
            if os.path.isfile(p) and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _search_in_zip(zip_path: str, query_lower: str, scope: str, limit_hits: int) -> Iterable[dict[str, Any]]:
    """
    単一ZIP内の検索（/data 限定）。ファイル名一致 / 内容一致（テキストのみ）。
    scope: "filename" | "content" | "both"
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

                # 内容一致（テキスト拡張子のみ）
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
# ディレクトリ検索ユーティリティ（/dataのみ）
# =========================

def _refresh_dir_roots() -> list[str]:
    """存在する検索ルート(/data)のみ抽出して保持。"""
    global _DIR_ROOTS_ACTUAL
    roots: list[str] = []
    for d in DIR_SEARCH_ROOTS_CANDIDATES:
        if os.path.isdir(d):
            roots.append(d)
    _DIR_ROOTS_ACTUAL = roots
    return roots


def _rebuild_dir_file_list() -> None:
    """
    /data 配下の対象ファイル（テキスト拡張子のみ）一覧を再構築してキャッシュ。
    """
    global _DIR_FILE_LIST, _DIR_FILE_LIST_STAMP

    roots = _refresh_dir_roots()
    files: list[str] = []
    total = 0

    for root in roots:
        for cur, _dirs, names in os.walk(root):
            # 過剰スキャン防止
            if total >= MAX_FILES_SCANNED:
                break
            for nm in names:
                if total >= MAX_FILES_SCANNED:
                    break
                path = os.path.join(cur, nm)
                if _is_text_ext(path):
                    files.append(path)
                total += 1

    _DIR_FILE_LIST = files
    _DIR_FILE_LIST_STAMP = time.time()
    if not roots:
        sys.stderr.write("[INDEX] WARNING: /data が存在しません。インデックスは空です。\n")
    sys.stderr.write(f"[INDEX] files={len(files)} roots={len(roots)} stamp={_DIR_FILE_LIST_STAMP}\n")


def _ensure_dir_file_list_recent(force: bool = False) -> None:
    """キャッシュが古い/空なら再構築。force=True で強制再構築。"""
    if force or (time.time() - _DIR_FILE_LIST_STAMP > INDEX_REFRESH_SEC) or not _DIR_FILE_LIST:
        _rebuild_dir_file_list()


def _search_in_file(file_path: str, query_lower: str, scope: str) -> Iterable[dict[str, Any]]:
    """
    単一ファイルの検索。ファイル名一致 / 内容一致（テキストのみ）。
    scope: "filename" | "content" | "both"
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

    # 内容一致（先頭 MAX_BYTES_PER_FILE バイト）
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
# 検索実体
# =========================

def perform_search(query: str,
                   mode: str = "dir",
                   scope: str = "both",
                   max_hits: int = MAX_HITS_RETURNED,
                   force_reindex: bool = False) -> dict[str, Any]:
    """
    総合検索関数。
    - query: 検索語（大小無視）
    - mode : "dir"(既定) | "zip" | "both" … いずれも /data 配下に限定
    - scope: "filename" | "content" | "both"
    - max_hits: 返却上限
    - force_reindex: ディレクトリインデックスの強制再構築
    """
    t0 = time.time()
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "empty_query", "message": "検索語が空です。何か入力してください。"}

    ql = query.lower()
    hits: list[dict[str, Any]] = []
    scanned_zip_count = 0

    # /data ディレクトリ検索
    if mode in ("dir", "both"):
        _ensure_dir_file_list_recent(force=force_reindex)
        for fp in _DIR_FILE_LIST:
            for h in _search_in_file(fp, ql, scope=scope):
                hits.append(h)
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break

    # /data 配下のZIP検索
    if len(hits) < max_hits and mode in ("zip", "both"):
        zip_paths = _discover_zip_paths()
        for zp in zip_paths:
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
        "query": query,
        "hits": hits,
        "stats": {
            "mode": mode,
            "scope": scope,
            "dir_files_considered": len(_DIR_FILE_LIST) if mode in ("dir", "both") else 0,
            "zip_files_considered": scanned_zip_count if mode in ("zip", "both") else 0,
            "elapsed_sec": round(dt, 3),
            "truncated": len(hits) >= max_hits,
            "search_root": "/data",
        }
    }


# =========================
# HTTP ハンドラ
# =========================

class IndexFileHandler(SimpleHTTPRequestHandler):
    """
    ルーティング:
      - /api/search : 検索API（/data 限定）
      - /api/reindex: ディレクトリファイル一覧を再構築
      - /api/ping   : ヘルスチェック
      - /, /index.html : /input.html に振替
      - その他: view/ 配下を静的配信
    """

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/search":
            return self.handle_api_search(parsed)
        if parsed.path == "/api/reindex":
            return self.handle_api_reindex()
        if parsed.path == "/api/ping":
            return self._send_json({"ok": True, "pong": True})

        if parsed.path in ("/", "/index.html"):
            self.path = "/input.html"
        return super().do_GET()

    # --- /api/search ---
    def handle_api_search(self, parsed):
        qs = parse_qs(parsed.query)
        q = unquote((qs.get("q", [""])[0] or "").strip())
        mode = (qs.get("mode", ["dir"])[0] or "dir").lower()       # 既定: dir（/data ディレクトリ）
        scope = (qs.get("scope", ["both"])[0] or "both").lower()   # 既定: both
        limit_s = (qs.get("limit", [str(MAX_HITS_RETURNED)])[0] or str(MAX_HITS_RETURNED))
        try:
            limit = max(1, min(int(limit_s), MAX_HITS_RETURNED))
        except Exception:
            limit = MAX_HITS_RETURNED

        force = (qs.get("force", ["false"])[0] or "false").lower() in ("1", "true", "yes")

        result = perform_search(q, mode=mode, scope=scope, max_hits=limit, force_reindex=force)
        return self._send_json(result)

    # --- /api/reindex ---
    def handle_api_reindex(self):
        _rebuild_dir_file_list()
        return self._send_json({"ok": True, "reindexed": True, "files": len(_DIR_FILE_LIST), "root": "/data"})

    # --- JSONユーティリティ ---
    def _send_json(self, data: Dict[str, Any], status: int = 200):
        try:
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        except Exception as e:
            status = 500
            body = json.dumps({"ok": False, "error": "json_dump_failed", "message": str(e)}).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[HTTP] " + fmt % args + "\n")


# =========================
# サーバ起動
# =========================

def find_free_server():
    """空きポートを見つけて HTTP サーバを生成。"""
    last_error: Exception | None = None
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
    # 起動時に /data のインデックスを構築
    _rebuild_dir_file_list()

    if not os.path.isdir(VIEW_DIR):
        print(f"[ERROR] view フォルダが見つかりません: {VIEW_DIR}")
        sys.exit(1)

    for name in ("input.html", "app.js", "style.css"):
        path = os.path.join(VIEW_DIR, name)
        if not os.path.isfile(path):
            print(f"[WARN] {name} が見つかりません: {path}")

    httpd, port = find_free_server()
    url = f"http://{HOST}:{port}/"

    print("====================================")
    print(" Local Web Server")
    print("====================================")
    print(f" Project : {BASE_DIR}")
    print(f" Serve   : {VIEW_DIR}  ( / → input.html )")
    print(f" URL     : {url}")
    print(" Search  : Root = /data")
    print(" API     : GET /api/search?q=キーワード&mode=dir|zip|both&scope=filename|content|both")
    print("          GET /api/reindex  ( /data のファイル一覧を再構築 )")
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
