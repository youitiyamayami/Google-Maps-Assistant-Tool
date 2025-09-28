from typing import Dict
from .route_parser import RouteResult

def _bucket_label(mins: int | None, level: str, cfg: Dict) -> str:
    """
    「小/中/大」に応じて S/M/L ラベルを軽く付ける（見た目に少し加工）
    小: S<=10, M<=20
    中: S<=15, M<=30
    大: S<=20, M<=40
    """
    if mins is None:
        return ""
    ui = cfg["ui"]
    if level == "小":
        s, m = ui["bucket_small_s"], ui["bucket_small_m"]
    elif level == "中":
        s, m = ui["bucket_mid_s"], ui["bucket_mid_m"]
    else:
        s, m = ui["bucket_large_s"], ui["bucket_large_m"]

    if mins <= s:
        tag = "S"
    elif mins <= m:
        tag = "M"
    else:
        tag = "L"
    return f" [{tag}]"

def format_route_text(route: RouteResult, bucket_level: str, cfg: Dict) -> str:
    """
    見やすさ優先で、軽い見た目加工を行ったテキストを返す。
    - 取得文字列の“内容”は改変せず、区切りや見出しをつけるのみ
    - 所要分が取れた場合は S/M/L の簡易ラベルを付与
    """
    out = []
    out.append("======================================")
    out.append("  Googleマップ ルート抽出（MVP）")
    out.append("======================================")
    out.append(f"出発地: {route.origin}")
    if route.waypoints:
        out.append(f"中継地: {' | '.join(route.waypoints)}")
    else:
        out.append("中継地: （なし）")
    out.append(f"到着地: {route.destination}")
    out.append("--------------------------------------")
    out.append(f"出発時刻: {route.depart_time or '不明'}")
    out.append(f"到着時刻: {route.arrive_time or '不明'}")
    out.append(f"合計運賃: {route.fare_total or '不明'}")
    out.append("")

    # 区間ごと
    for leg in route.legs:
        out.append(f"=== 区間 {leg.index} ===")
        if leg.duration_min is not None:
            out.append(f"推定所要: {leg.duration_min}分{_bucket_label(leg.duration_min, bucket_level, cfg)}")
        if leg.fare_text:
            out.append(f"運賃目安: {leg.fare_text}")
        out.append("-- 抽出テキスト --")
        # 元テキストは改変せず、行そのまま列挙
        for ln in leg.raw_lines:
            out.append(f"  {ln}")
        out.append("")

    out.append("--------------------------------------")
    out.append("【全体抽出テキスト（参考）】")
    out.append(route.raw_text.strip())
    out.append("")
    return "\n".join(out)
