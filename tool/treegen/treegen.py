# tools/treegen.py
# -*- coding: utf-8 -*-
"""
プロジェクトの階層図を .gitignore に従って生成し、
毎回 固定ファイル に上書き保存するスクリプト。

追加機能:
- このファイル内で「除外したいフォルダ/ファイル」を定義可能（INTERNAL_IGNORE_*）
- --no-internal-ignores で py内定義の除外をオフにできます

既定の出力先: <root>/PROJECT_TREE.txt
  - 変更したい場合: --out でファイルパスを指定（絶対/相対どちらでも可）
  - 例: --out docs/STRUCTURE.txt, --out "C:/path/to/tree.txt"

Usage 例:
  # ルート直下に PROJECT_TREE.txt を上書き保存（深さ無制限）
  py tools/treegen.py --root . --max-depth 0

  # 4階層まで、出力先を明示
  py tools/treegen.py --root . --max-depth 4 --out docs/PROJECT_TREE.txt

※ より厳密な .gitignore 解釈をしたい場合:
  py -m pip install pathspec
"""

import os
import sys
import argparse
import fnmatch

BOX_MID  = "├─ "
BOX_END  = "└─ "
BOX_PIPE = "│  "
BOX_PAD  = "   "

# 既定の“お作法”除外（.git, venv 等）
DEFAULT_IGNORES = [
    ".git", ".gitmodules", ".gitignore", ".gitattributes",
    "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".vscode", ".idea", "dist", "build", ".DS_Store"
]

# ==== ここを編集するだけで、常に除外できます ============================
# 例:
#   INTERNAL_IGNORE_DIRS  = ["debug_shots", "image/sorce", "logs"]
#   INTERNAL_IGNORE_FILES = ["*.png", "tools/project_tree.txt"]
INTERNAL_IGNORE_DIRS: list[str] = [
    "image/sorce",
    "config/properties",
    # 例："config",
]
INTERNAL_IGNORE_FILES: list[str] = [
    "debug_shots/*.*",
    "config/config.yaml",
    "project_memo.txt",
    "grab_region.py",
    "create_templates.py",
    # 例: "*.png", "tools/project_tree.txt"
]
# =====================================================================

def load_lines(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return [ln.rstrip("\n") for ln in f]
    except FileNotFoundError:
        return []

def load_gitignore_lines(root: str, gitignore: str) -> list[str]:
    """
    ルート直下の .gitignore を読み取り、有効行のみ返す（# と空行は除外）
    """
    path = gitignore if os.path.isabs(gitignore) else os.path.join(root, gitignore)
    lines = load_lines(path)
    results = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        results.append(s)
    return results

def build_internal_patterns(dirs: list[str], files: list[str]) -> list[str]:
    """
    py内定義の除外リストから、gitignore風のパターンに展開。
    - ディレクトリは 4 つのパターンを登録: "d", "d/", "**/d", "**/d/"
    - ファイルは 2 つのパターンを登録: "f", "**/f"（ワイルドカード可）
    """
    pats: list[str] = []
    for d in dirs:
        d = d.replace("\\", "/").strip().strip("/")
        if not d:
            continue
        pats.extend([f"{d}", f"{d}/", f"**/{d}", f"**/{d}/"])
    for f in files:
        f = f.replace("\\", "/").strip()
        if not f:
            continue
        pats.extend([f"{f}", f"**/{f}"])
    return pats

def build_default_patterns() -> list[str]:
    """
    DEFAULT_IGNORES をファイル/ディレクトリ両対応で展開。
    例: ".git" -> ".git", ".git/", "**/.git", "**/.git/"
        ".gitignore" -> ".gitignore", "**/.gitignore"
    """
    pats: list[str] = []
    for p in DEFAULT_IGNORES:
        p = p.replace("\\", "/").strip().strip("/")
        if not p:
            continue
        # ファイル/ディレクトリ両想定
        pats.extend([p, f"**/{p}"])
        # ディレクトリ想定（末尾/）
        pats.extend([f"{p}/", f"**/{p}/"])
    return pats

def _matches_fallback(pat: str, rel: str, base: str, is_dir: bool) -> bool:
    """
    pathspec が無い場合の簡易マッチ。末尾の / と 先頭の **/ に寛容。
    - "name/" と "name" の両方を試す
    - "**/name" と "name" の両方を試す
    """
    p = pat.replace("\\", "/").strip()
    if not p:
        return False

    # ディレクトリ専用パターン（末尾/）はファイルには適用しない
    if p.endswith("/") and not is_dir:
        return False

    # 末尾スラッシュは「有り/無し」両方を試す
    variants = {p.rstrip("/")}
    variants.add(p.rstrip("/") + "/")  # ベース名判定用にも残す

    # 先頭の **/ があってもなくても試す
    new_variants = set()
    for q in variants:
        new_variants.add(q)
        if q.startswith("**/"):
            new_variants.add(q[3:])
        else:
            new_variants.add("**/" + q)
    variants = new_variants

    # 実際の照合
    for q in variants:
        if fnmatch.fnmatch(rel, q) or fnmatch.fnmatch(base, q):
            return True
    return False

class IgnoreMatcher:
    """
    .gitignore / 既定除外 / 追加ignore / py内定義ignore をまとめて判定。
    - pathspec がある場合は gitwildmatch で厳密に
    - 無い場合は簡易 fnmatch（末尾/ と **/ に寛容化）
    """
    def __init__(
        self,
        root: str,
        gitignore_lines: list[str],
        extra_ignores: list[str],
        use_default: bool,
        internal_patterns: list[str]
    ):
        self.root = root
        self.use_pathspec = False
        self.spec = None

        lines: list[str] = []
        if use_default:
            lines.extend(build_default_patterns())
        # py内定義（INTERNAL_IGNORE_*）
        lines.extend(internal_patterns)
        # 追加ignore（--ignore）
        lines.extend(extra_ignores)
        # .gitignore
        lines.extend(gitignore_lines)

        self.active = bool(lines)
        self.lines = [ln.strip() for ln in lines if ln.strip()]

        try:
            import pathspec  # type: ignore
            self.spec = pathspec.PathSpec.from_lines("gitwildmatch", self.lines)
            self.use_pathspec = True
        except Exception:
            self.use_pathspec = False

    def ignored(self, full_path: str, is_dir: bool) -> bool:
        if not self.active:
            return False
        rel = os.path.relpath(full_path, self.root).replace("\\", "/")
        base = os.path.basename(full_path)
        if self.use_pathspec:
            return self.spec.match_file(rel)
        else:
            for pat in self.lines:
                if _matches_fallback(pat, rel, base, is_dir):
                    return True
            return False

def list_children(parent: str, matcher: IgnoreMatcher, include_files: bool) -> list[str]:
    try:
        entries = os.listdir(parent)
    except PermissionError:
        return []
    visible = []
    for name in entries:
        full = os.path.join(parent, name)
        is_dir = os.path.isdir(full)
        if matcher.ignored(full, is_dir):
            continue
        if not include_files and not is_dir:
            continue
        visible.append(name)
    # ディレクトリ→ファイルの順、同種内はアルファベット順
    visible.sort(key=lambda s: (not os.path.isdir(os.path.join(parent, s)), s.lower()))
    return visible

def append_tree_lines(root: str, max_depth: int | None, matcher: IgnoreMatcher,
                      include_files: bool, prefix: str, buf: list[str]):
    children = list_children(root, matcher, include_files)
    for i, name in enumerate(children):
        full = os.path.join(root, name)
        is_dir = os.path.isdir(full)
        connector = BOX_END if i == len(children) - 1 else BOX_MID
        buf.append(prefix + connector + name)
        if is_dir and (max_depth is None or max_depth > 1):
            next_prefix = prefix + (BOX_PAD if i == len(children) - 1 else BOX_PIPE)
            next_depth = None if max_depth is None else max_depth - 1
            append_tree_lines(full, next_depth, matcher, include_files, next_prefix, buf)

def render_tree_text(root: str, max_depth: int | None, matcher: IgnoreMatcher, include_files: bool) -> str:
    lines: list[str] = []
    # 先頭にルート名
    lines.append(os.path.basename(root) or root)
    append_tree_lines(root, max_depth, matcher, include_files, prefix="", buf=lines)
    return "\n".join(lines) + "\n"

def ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def main():
    ap = argparse.ArgumentParser()
    # 既定値は r"..." の生文字列にして Windows パスのエスケープ問題を回避
    ap.add_argument("--root", default=r"C:\Users\Kaito_Tanaka\Desktop\private_file\kancolle_data\kancolle_program", help="起点ディレクトリ（既定: .）")
    ap.add_argument("--max-depth", type=int, default=0, help="最大深さ。0以下なら無制限")
    ap.add_argument("--dirs-only", action="store_true", help="ディレクトリのみ表示")
    ap.add_argument("--ignore", default="", help="カンマ区切りの追加ignore（glob可）例: 'config/log,*.log'")
    ap.add_argument("--gitignore", default=".gitignore", help="読み込む .gitignore のパス（既定: ルート直下）")
    ap.add_argument("--no-gitignore", action="store_true", help=".gitignore を無視する")
    ap.add_argument("--no-default-ignores", action="store_true", help="既定の除外リストを使わない")
    ap.add_argument("--no-internal-ignores", action="store_true", help="py内定義の除外（INTERNAL_IGNORE_*）を無効化")
    ap.add_argument("--out", default=r".\tools\treegen\PROJECT_TREE.txt", help="出力ファイル（既定: <root>/PROJECT_TREE.txt）")
    ap.add_argument("--encoding", default="utf-8", help="出力エンコーディング（既定: utf-8。Notepad用は utf-8-sig）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    max_depth = None if args.max_depth <= 0 else args.max_depth
    extra_ignores = [p.strip() for p in args.ignore.split(",") if p.strip()]

    gitignore_lines = []
    if not args.no_gitignore:
        gitignore_lines = load_gitignore_lines(root, args.gitignore)

    internal_patterns: list[str] = []
    if not args.no_internal_ignores:
        internal_patterns = build_internal_patterns(INTERNAL_IGNORE_DIRS, INTERNAL_IGNORE_FILES)

    matcher = IgnoreMatcher(
        root=root,
        gitignore_lines=gitignore_lines,
        extra_ignores=extra_ignores,
        use_default=not args.no_default_ignores,
        internal_patterns=internal_patterns,
    )

    text = render_tree_text(root, max_depth, matcher, include_files = (not args.dirs_only))  # keep arg name compatibility

    # 出力先を決定（相対パスなら <root>/ を基準にする）
    out_path = args.out
    if not os.path.isabs(out_path):
        out_path = os.path.join(root, out_path)

    ensure_parent_dir(out_path)
    with open(out_path, "w", encoding=args.encoding, newline="\n") as f:
        f.write(text)

    # 進行ログ（標準出力）
    print(f"Wrote: {out_path}")

if __name__ == "__main__":
    sys.exit(main())
