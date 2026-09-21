from pathlib import Path

import pytest

from phoenix.envfile import load_dotenv


def test_loads_parses_and_never_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY", "E_KEY", "EXISTING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EXISTING", "from-shell")
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nA_KEY=plain\nB_KEY = \"quoted value\"\nexport C_KEY='single'\n"
        "D_KEY=\nEXISTING=from-file\nnot a line\nE_KEY=a=b\n",
        encoding="utf8",
    )
    loaded = load_dotenv(env)
    import os

    assert os.environ["A_KEY"] == "plain"
    assert os.environ["B_KEY"] == "quoted value"
    assert os.environ["C_KEY"] == "single"
    assert "D_KEY" not in os.environ  # blank values are ignored
    assert os.environ["EXISTING"] == "from-shell"  # real environment wins
    assert os.environ["E_KEY"] == "a=b"
    assert sorted(loaded) == ["A_KEY", "B_KEY", "C_KEY", "E_KEY"]
    for k in loaded:
        monkeypatch.delenv(k)


def test_missing_file_is_fine(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "nope.env") == []
