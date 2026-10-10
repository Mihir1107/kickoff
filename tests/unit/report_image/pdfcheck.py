"""Structural reading of a report PDF with pypdf (ADR 0018 §15 "PDF structure")."""

from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

BANNED = ("/JavaScript", "/JS", "/OpenAction", "/AA", "/Launch", "/URI", "/EmbeddedFile",
          "/EmbeddedFiles", "/SubmitForm", "/GoToR")  # fmt: skip


def reader(pdf: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(pdf))


def all_keys(r: PdfReader) -> set[str]:
    """Every dictionary key reachable from the trailer (object streams included: pypdf resolves them)."""
    seen: set[int] = set()
    keys: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, IndirectObject):
            if obj.idnum in seen:
                return
            seen.add(obj.idnum)
            obj = obj.get_object()
        if isinstance(obj, DictionaryObject):
            for k, v in obj.items():
                keys.add(str(k))
                walk(v)
        elif isinstance(obj, ArrayObject):
            for v in obj:
                walk(v)

    walk(r.trailer)
    return keys


def fonts(r: PdfReader) -> set[str]:
    out: set[str] = set()
    for page in r.pages:
        res = page.get("/Resources") or {}
        for f in (res.get("/Font") or {}).values():
            fd = f.get_object()
            base = str(fd.get("/BaseFont"))
            for d in fd.get("/DescendantFonts") or []:
                base = str(d.get_object().get("/BaseFont"))
            out.add(base)
    return out


def texts(r: PdfReader) -> list[str]:
    return [p.extract_text() or "" for p in r.pages]
