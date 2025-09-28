# modules/route_parser.py
import re
from dataclasses import dataclass, field
from typing import List, Optional

# ---- データ構造（MVP改良版） ----
@dataclass
class Leg:
    index: int
    raw_lines: List[str] = field(default_factory=list)
    duration_min: Optional[int] = None   # 解析で拾えたら
    fare_text: Optional[str] = None      # 解析で拾えたら

@dataclass
class RouteResult:
    origin: str
    destination: str
    waypoints: List[str]
    depart_time: Optional[str] = None
    arrive_time: Optional[str] = None
    legs: List[Leg] = field(default_factory=list)
    fare_total: Optional[str] = None
    raw_text: str = ""  # 最後に参考として全体生テキスト

# ---- 正規表現（ja前提） ----
_RE_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RE_RANGE = re.compile(r"\b([01]?\d:[0-5]\d)\s*[-–~]\s*([01]?\d:[0-5]\d)\b")
_RE_MIN  = re.compile(r"(\d+)\s*分")
_RE_YEN  = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s*円")

def _extract_times(lines: List[str]):
    """
    まず 'HH:MM - HH:MM' の範囲表記を優先。
    なければ全時刻から最初/最後を取る。最初と最後が同じなら、
    別の時刻が存在すれば末尾の別時刻を到着に採用する。
    """
    for ln in lines:
        m = _RE_RANGE.search(ln)
        if m:
            return m.group(1), m.group(2)

    found = []
    for ln in lines:
        found.extend([m.group(0) for m in _RE_TIME.finditer(ln)])
    if not found:
        return None, None
    dep = found[0]
    arr = found[-1]
    if dep == arr and len(found) > 1:
        # 後方から別時刻を探す
        for t in reversed(found):
            if t != dep:
                arr = t
                break
    return dep, arr

def _first_index_containing(lines: List[str], token: str) -> Optional[int]:
    token = token.strip()
    if not token:
        return None
    for i, ln in enumerate(lines):
        if token in ln:
            return i
    return None

def _split_legs_by_waypoints(lines: List[str], origin: str, waypoints: List[str], destination: str) -> List[List[str]]:
    """
    分割の安定性向上：各マーカー（origin, waypoints..., destination）の
    最初に出現する位置で区切る。繰り返し出現に影響されない。
    マーカーのうち見つかったものだけで分割し、最低1レグは返す。
    """
    anchors: List[int] = []
    for mark in [origin] + waypoints + [destination]:
        idx = _first_index_containing(lines, mark) if mark else None
        if idx is not None:
            anchors.append(idx)

    if not anchors:
        return [lines]

    anchors = sorted(set(anchors))
    # 先頭/末尾を明示
    if anchors[0] != 0:
        anchors = [0] + anchors
    if anchors[-1] != len(lines) - 1:
        anchors = anchors + [len(lines) - 1]

    # スライスを作成
    legs: List[List[str]] = []
    for i in range(len(anchors) - 1):
        start = anchors[i]
        end = anchors[i + 1]
        seg = [x for x in lines[start:end + 1] if x.strip()]
        if seg:
            legs.append(seg)

    # 期待本数より極端に多い場合（ノイズ混入）は1レグにまとめる
    expected = max(1, len(waypoints) + 1)
    if len(legs) > expected * 2:  # しきい値は緩め
        return [lines]

    return legs if legs else [lines]

def _pick_duration_min(lines: List[str]) -> Optional[int]:
    # 最初に見つかった「～分」を採用（MVP）
    for ln in lines:
        m = _RE_MIN.search(ln)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None

def _pick_fare_text(lines: List[str]) -> Optional[str]:
    # 最初に見つかった「～円」を採用（MVP）
    for ln in lines:
        m = _RE_YEN.search(ln)
        if m:
            return m.group(0)
    return None

def parse_route_from_page(page_text: str, origin: str, destination: str, waypoints: List[str], language: str = "ja") -> RouteResult:
    """
    全体テキストから関連行だけを整形した上でルートを抽出。
    - 出発/到着時刻：範囲表記があれば優先（HH:MM - HH:MM）
    - 区間分割：各マーカーの「初出位置」でスライス（重複出現の影響を排除）
    - 各レグの所要分/運賃：最初に見つかった値を採用
    """
    # 前処理：空行・重空白除去
    lines = []
    for ln in page_text.splitlines():
        s = " ".join(ln.split()).strip()
        if s:
            lines.append(s)

    depart, arrive = _extract_times(lines)
    raw = page_text

    legs_raw = _split_legs_by_waypoints(lines, origin, waypoints, destination)
    legs: List[Leg] = []
    for i, seg in enumerate(legs_raw, 1):
        legs.append(Leg(
            index=i,
            raw_lines=seg,
            duration_min=_pick_duration_min(seg),
            fare_text=_pick_fare_text(seg),
        ))

    # 総額らしきもの（後方優先）
    fare_total = None
    for ln in reversed(lines):
        m = _RE_YEN.search(ln)
        if m:
            fare_total = m.group(0)
            break

    return RouteResult(
        origin=origin,
        destination=destination,
        waypoints=waypoints,
        depart_time=depart,
        arrive_time=arrive,
        legs=legs,
        fare_total=fare_total,
        raw_text=raw
    )
