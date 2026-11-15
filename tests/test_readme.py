"""Comparison table: hosted readers do not keep data on the machine."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _table_row(text: str, label: str) -> list[str]:
    prefix = f"| {label} |"
    for line in text.splitlines():
        if line.startswith(prefix):
            return [part.strip() for part in line.strip().strip("|").split("|")]
    raise AssertionError(f"missing comparison row {label!r}")


def test_hosted_readers_do_not_keep_data_on_the_machine() -> None:
    """Readwise and Feedly are hosted; a cache is not on-machine storage."""
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    want = ["yes", "varies", "no (hosted)", "no (hosted)"]
    assert _table_row(english, "data stays on your machine") == [
        "data stays on your machine",
        *want,
    ]
    assert _table_row(chinese, "数据留在你的机器上") == ["数据留在你的机器上", *want]
    assert "| local storage |" not in english
    assert "| 本地存储 |" not in chinese
