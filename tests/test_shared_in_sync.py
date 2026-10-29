"""The app bar and typeface live in /shared; every app's copy must match."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = ["house/house/www/shared", "music/music/www/shared", "frame/shared"]


def test_copies_match_shared():
    for name in ("appnav.js", "tokens.css", "archivo.woff2"):
        src = (ROOT / "shared" / name).read_bytes()
        for c in COPIES:
            assert (ROOT / c / name).read_bytes() == src, f"{c}/{name} drifted: run tools/sync-shared"
