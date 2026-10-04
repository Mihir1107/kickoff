"""Zip names (ADR 0015 §4): flat, safe, NFC, at most 200 bytes, extension kept."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from edisc_renderers.rsmf.names import attachment_name, placeholder_name


def test_examples() -> None:
    assert attachment_name("F1", "report.pdf") == "F1_report.pdf"
    assert attachment_name("F1", "../../etc/passwd") == "F1__.._etc_passwd"
    assert attachment_name("F1", 'a\\b:c*d?e"f<g>h|i.txt') == "F1_a_b_c_d_e_f_g_h_i.txt"
    assert attachment_name("F1", "tab\there\x00nul.txt") == "F1_tab_here_nul.txt"
    assert attachment_name("F1", "café.txt") == "F1_café.txt"  # NFC
    assert attachment_name("F1", "zero​width.txt") == "F1_zero_width.txt"
    assert attachment_name("F1", "no\u00a0break\u3000space.txt") == "F1_no_break_space.txt"
    assert attachment_name("F1", "") == "F1_file"
    assert attachment_name("F1", " . ") == "F1_file"
    assert attachment_name("F1", ".bashrc") == "F1_bashrc"
    long = attachment_name("F1", "李" * 300 + ".docx")
    assert long.endswith(".docx") and len(long.encode()) <= 200
    assert placeholder_name("F1") == "F1_UNAVAILABLE.txt"


@given(st.text(max_size=400))
def test_any_name_is_safe(original: str) -> None:
    name = attachment_name("F0ABC", original)
    assert name.startswith("F0ABC_")
    assert len(name.encode("utf-8")) <= 200
    assert not any(c in name for c in '/\\:*?"<>|')
    assert all(c.isprintable() or c == " " for c in name)
    assert name == attachment_name("F0ABC", original)
