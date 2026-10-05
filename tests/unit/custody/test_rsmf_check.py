"""The verifier's streaming `.rsmf` reader (ADR 0015 §20.6): random access to the base64 zip part, the
native references read from the manifest, and each placeholder checked against its reference."""

from __future__ import annotations

import binascii
import email
import email.policy
import hashlib
import io
import random
import zipfile
from datetime import date
from pathlib import Path

import pytest

from edisc_custody.rsmf_check import Base64Zip, FileRange, RsmfCheckError, _run, external_refs
from edisc_renderers.rsmf import FileAttachment, RenderOptions, render_slice
from tests.unit.renderers.builders import attachment, msg, opener_for, ref, slice_input

MIB = 1 << 20


def _render(inline: bytes) -> tuple[bytes, FileAttachment]:
    big = FileAttachment(
        "F1", "big.bin", 3 * MIB, hashlib.sha256(b"F1").hexdigest(), ref("T0TEST/file/F1"), "F1"
    )
    files = {"F1": big, "F2": attachment("F2", "small.bin", inline)}
    m = msg("2026-01-05T09:00:00Z", files=("F1", "F2"))
    [f] = render_slice(
        slice_input(date(2026, 1, 5), [m], files=files), RenderOptions(external_over_bytes=MIB)
    )
    return b"".join(f.stream(opener_for({"F2": inline}))), big


def _zip_of(eml: bytes) -> bytes:
    msg = email.message_from_bytes(eml, policy=email.policy.default)
    return bytes(next(iter(msg.iter_attachments())).get_content())


def _rewrap(eml: bytes, new_zip: bytes) -> bytes:
    """The same EML with another `rsmf.zip` in the same base64 layout."""
    marker = b'filename="rsmf.zip"\r\n\r\n'
    head = eml[: eml.index(marker) + len(marker)]
    tail = eml[eml.rindex(b"--rsmf-") :]
    lines = [
        binascii.b2a_base64(new_zip[i : i + 57], newline=False) + b"\r\n"
        for i in range(0, len(new_zip), 57)
    ]
    return head + b"".join(lines) + tail


def _edit_zip(blob: bytes, name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, data if info.filename == name else src.read(info))
    return out.getvalue()


def _write(tmp_path: Path, data: bytes) -> FileRange:
    p = tmp_path / "x.rsmf"
    p.write_bytes(data)
    return FileRange(p)


def test_the_base64_view_reads_any_range(tmp_path: Path) -> None:
    rng = random.Random(7)
    for n in (0, 1, 56, 57, 58, 113, 114, 400):
        eml, _ = _render(bytes(rng.randrange(256) for _ in range(n)))
        blob = _zip_of(eml)
        src = Base64Zip(_write(tmp_path, eml))
        assert src.size == len(blob)
        for off in range(0, len(blob), 11):
            for length in (0, 1, 56, 57, 58, 300):
                length = min(length, len(blob) - off)
                assert _run(src.read(off, length)) == blob[off : off + length]


def test_the_references_and_placeholders_are_read(tmp_path: Path) -> None:
    eml, big = _render(b"inline")
    [r] = external_refs(_write(tmp_path, eml))
    assert (r.file_id, r.sha256, r.size) == ("F1", big.sha256, big.size)


def test_a_placeholder_that_disagrees_with_its_reference_fails(tmp_path: Path) -> None:
    eml, big = _render(b"inline")
    text = (
        f"name: big.bin\nsize: {big.size}\nsha256: {'0' * 64}\nreason: over_external_threshold\n"
        f"native: natives/{'0' * 64}\n"
    ).encode()
    tampered = _rewrap(eml, _edit_zip(_zip_of(eml), "F1_EXTERNAL.txt", text))
    with pytest.raises(RsmfCheckError, match="disagrees"):
        external_refs(_write(tmp_path, tampered))
    crlf = _zip_of(eml)
    with zipfile.ZipFile(io.BytesIO(crlf)) as z:
        original = z.read("F1_EXTERNAL.txt")
    tampered = _rewrap(eml, _edit_zip(crlf, "F1_EXTERNAL.txt", original.replace(b"\n", b"\r\n")))
    with pytest.raises(RsmfCheckError, match="layout"):
        external_refs(_write(tmp_path, tampered))


def test_a_reference_without_a_placeholder_fails(tmp_path: Path) -> None:
    eml, _ = _render(b"inline")
    blob = _zip_of(eml)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename != "F1_EXTERNAL.txt":
                dst.writestr(info.filename, src.read(info))
    with pytest.raises(RsmfCheckError, match="no placeholder"):
        external_refs(_write(tmp_path, _rewrap(eml, out.getvalue())))


@pytest.mark.parametrize("damage", ["short_line", "bad_char", "no_tail", "no_part"])
def test_a_damaged_envelope_fails(tmp_path: Path, damage: str) -> None:
    eml, _ = _render(b"x" * 500)
    marker = b'filename="rsmf.zip"\r\n\r\n'
    start = eml.index(marker) + len(marker)
    if damage == "short_line":  # one line of 75 characters before the last
        eml = eml[: start + 10] + eml[start + 11 :]
    elif damage == "bad_char":
        eml = eml[: start + 5] + b"*" + eml[start + 6 :]
    elif damage == "no_tail":
        eml = eml[:-5]
    else:
        eml = eml.replace(marker, b'filename="other.zip"\r\n\r\n')
    with pytest.raises(RsmfCheckError):
        external_refs(_write(tmp_path, eml))


@pytest.mark.parametrize("field", ["sha256", "native", "reason", "size"])
def test_each_placeholder_field_is_checked(tmp_path: Path, field: str) -> None:
    eml, big = _render(b"inline")
    blob = _zip_of(eml)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        lines = z.read("F1_EXTERNAL.txt").decode().splitlines()
    wrong = {"sha256": "1" * 64, "native": f"natives/{'1' * 64}", "reason": "too_big", "size": "x"}
    text = "".join(
        f"{k}: {wrong[field]}\n" if k == field else f"{line}\n"
        for line in lines
        for k in [line.split(": ", 1)[0]]
    ).encode()
    tampered = _rewrap(eml, _edit_zip(blob, "F1_EXTERNAL.txt", text))
    with pytest.raises(RsmfCheckError, match="disagrees"):
        external_refs(_write(tmp_path, tampered))
    assert big.size > 0


def test_a_line_not_ending_in_crlf_fails_even_if_it_decodes(tmp_path: Path) -> None:
    """The CRLF after the first line replaced by two valid base64 characters: the same length, the
    same decoded zip if the terminator were simply dropped, but not the layout we write."""
    eml, _ = _render(b"x" * 500)
    marker = b'filename="rsmf.zip"\r\n\r\n'
    end = eml.index(marker) + len(marker) + 76
    assert eml[end : end + 2] == b"\r\n"
    with pytest.raises(RsmfCheckError, match="CRLF"):
        external_refs(_write(tmp_path, eml[:end] + b"AA" + eml[end + 2 :]))
