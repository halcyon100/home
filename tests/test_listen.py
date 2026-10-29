import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "music" / "music"))
import listen  # noqa: E402


class FakeHouse:
    opts = {}


def test_spotify_url_from_ma_uri():
    assert listen.spotify_url("spotify--opL497CN://album/3gralcjkI8QCnIgCmlJ6RP", "a", "b") == \
        "https://open.spotify.com/album/3gralcjkI8QCnIgCmlJ6RP"
    assert listen.spotify_url("library://album/1706", "Boards of Canada", "Inferno").startswith(
        "https://open.spotify.com/search/Boards%20of")


def test_artist_match_is_forgiving_but_not_loose():
    assert listen.artist_matches("Efdemin", ["efdemin"])
    assert listen.artist_matches("John Berndt", ["M.C. Schmidt", "John Berndt"])
    assert not listen.artist_matches("Tycho", ["Tyconic"])


def test_due_on_friday_morning(tmp_path):
    l = listen.Listen(FakeHouse(), str(tmp_path / "l.json"))
    assert l.due(datetime(2026, 10, 29, 12))                     # nothing yet
    l.data["picks"] = [{"id": "x"}]
    l.data["generated"] = "2026-10-29T12:00"                     # Thursday
    assert not l.due(datetime(2026, 10, 30, 6, 59))              # Friday, before 7
    assert l.due(datetime(2026, 10, 30, 7, 5))                   # Friday, after 7
    l.data["generated"] = "2026-10-30T07:05"
    assert not l.due(datetime(2026, 11, 5, 23))                  # following Thursday


def test_dismiss_hides_without_judging(tmp_path):
    l = listen.Listen(FakeHouse(), str(tmp_path / "l.json"))
    l.data["picks"] = [{"id": "a", "artist": "A", "album": "X", "kind": "new"},
                       {"id": "b", "artist": "B", "album": "Y", "kind": "new"}]
    l.feedback("a", "dismiss")
    l.feedback("b", "up")
    v = l.view()
    assert [p["id"] for p in v["picks"]] == ["b"]
    prompt = l._prompt([], [], datetime(2026, 10, 30))
    assert "LIKED: B - Y" in prompt and "DISLIKED: none yet" in prompt and "A - X" not in prompt.split("LIKED")[1]
