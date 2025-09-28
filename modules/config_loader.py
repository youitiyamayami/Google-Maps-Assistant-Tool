import os
from typing import Dict, Any

try:
    import tomli as toml  # Python3.11以降にtomllibでもOKだが指示に沿ってtomli/tomlを使用
except Exception:
    import toml

DEFAULT_PATH = "config.default.toml"

_DEFAULTS = {
    "app": {
        "language": "ja",
        "region": "JP",
        "headless": False,
        "timeout_sec": 30,
        "encoding": "utf-8",
        "random_sleep_ms": [200, 600],
    },
    "ui": {
        "bucket_small_s": 10,
        "bucket_small_m": 20,
        "bucket_mid_s": 15,
        "bucket_mid_m": 30,
        "bucket_large_s": 20,
        "bucket_large_m": 40,
    }
}

def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def load_config(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    if os.path.exists(path):
        with open(path, "rb") as f:
            loaded = toml.load(f)
        cfg = _deep_merge(cfg, loaded)
    return cfg
