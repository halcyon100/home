"""The house guide: saved on the box, earlier versions kept, never empty."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "house" / "house"))
from guide import KEEP, Guide  # noqa: E402


def test_no_guide_yet(tmp_path):
    g = Guide(tmp_path / "guide")
    assert g.read() is None and g.versions() == []


def test_save_keeps_the_previous_version(tmp_path):
    g = Guide(tmp_path / "guide")
    assert g.save("# One")["changed"]
    assert g.save("# One")["changed"] is False          # same text: nothing to keep
    g.save("# Two")
    assert g.read() == "# Two"
    [old] = g.versions()
    assert (g.old / old).read_text() == "# One"


def test_versions_are_capped(tmp_path, monkeypatch):
    g = Guide(tmp_path / "guide")
    ticks = iter(range(1_700_000_000, 1_700_000_000 + 10_000, 60))
    monkeypatch.setattr(g, "updated", lambda: next(ticks))
    for n in range(KEEP + 5):
        g.save(f"# Version {n}")
    assert len(g.versions()) == KEEP


def test_refuses_empty_and_huge(tmp_path):
    g = Guide(tmp_path / "guide")
    with pytest.raises(ValueError):
        g.save("   ")
    with pytest.raises(ValueError):
        g.save("x" * (600 * 1024))
    assert g.read() is None


def test_edits_apply_all_or_nothing(tmp_path):
    g = Guide(tmp_path / "guide")
    g.save("# House\n\nThe stairs go dark at sunset.\n")
    g.edit([{"find": "go dark at sunset", "replace": "go dark 30 minutes after sunset"}])
    assert "30 minutes after sunset" in g.read()
    with pytest.raises(ValueError):
        g.edit([{"find": "House", "replace": "Home"}, {"find": "nowhere", "replace": "x"}])
    assert g.read().startswith("# House")          # the first edit didn't land either
