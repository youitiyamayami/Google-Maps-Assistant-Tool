import urllib.parse
import time
from dateutil import parser as dtparser

def _encode(s: str) -> str:
    return urllib.parse.quote_plus(s)

def _departure_time_param(depart_at: str | None) -> str:
    """
    Google Maps の dir/?api=1 では、departure_time は 'now' か UNIX 秒が堅い。
    入力が未指定なら 'now'、指定があればローカル時刻として解釈し UNIX 秒へ。
    """
    if not depart_at:
        return "now"
    try:
        dt = dtparser.parse(depart_at)  # 例: "2025-09-14 18:00"
        # tz未指定ならローカル時刻として扱い、UNIX秒へ
        # time.mktime はローカル時刻を前提にエポック秒に変換
        epoch = int(time.mktime(dt.timetuple()))
        return str(epoch)
    except Exception:
        return "now"

def build_gmaps_url(origin: str, destination: str, waypoints: list[str],
                    mode: str = "transit", language: str = "ja", region: str = "JP",
                    depart_at: str | None = None) -> str:
    """
    https://www.google.com/maps/dir/?api=1&origin=...&destination=...&travelmode=transit
    &waypoints=a|b|c&hl=ja&gl=JP&departure_time=now|<UNIX秒>
    """
    base = "https://www.google.com/maps/dir/?api=1"
    params = {
        "origin": origin,
        "destination": destination,
        "travelmode": mode,
        "hl": language,
        "gl": region,
        "departure_time": _departure_time_param(depart_at),
    }
    if waypoints:
        params["waypoints"] = "|".join(waypoints)

    q = "&".join(f"{k}={_encode(v)}" for k, v in params.items())
    return f"{base}&{q}"
