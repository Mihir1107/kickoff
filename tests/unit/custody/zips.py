"""Archive builders for the zip-reader tests: real zips (zipfile), byte-level tampering, and a lazily
generated archive with millions of entries that never exists in memory."""

from __future__ import annotations

import io
import struct
import zipfile

from edisc_custody.archive import CD_LEN, LOCAL_LEN


def make_zip(
    entries: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED, zip64: bool = False
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method, allowZip64=True) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 5, 0, 0, 0))
            info.compress_type = method
            with zf.open(info, "w", force_zip64=zip64) as fh:
                fh.write(data)
    return buf.getvalue()


def cd_offset(data: bytes) -> int:
    pos = data.rfind(b"PK\x05\x06")
    return int(struct.unpack_from("<I", data, pos + 16)[0])


def patch_cd(data: bytes, index: int, fmt: str, field_offset: int, value: int) -> bytes:
    """Overwrite a field of the index-th central-directory record."""
    pos = cd_offset(data)
    for _ in range(index):
        nlen, xlen, clen = struct.unpack_from("<HHH", data, pos + 28)
        pos += CD_LEN + nlen + xlen + clen
    out = bytearray(data)
    struct.pack_into(fmt, out, pos + field_offset, value)
    return bytes(out)


class SyntheticArchive:
    """A valid ZIP64 archive of ``n`` empty stored entries named ``d00000000.json`` ..., generated on
    read: memory use is constant whatever ``n`` is."""

    NAME_LEN = 14

    def __init__(self, n: int) -> None:
        self.n = n
        self.local_len = LOCAL_LEN + self.NAME_LEN
        self.cd_len = CD_LEN + self.NAME_LEN + 20  # + zip64 extra with the local header offset
        self.cd_offset = n * self.local_len
        self.cd_size = n * self.cd_len
        self.z64_offset = self.cd_offset + self.cd_size
        self.size = self.z64_offset + 56 + 20 + 22
        self.reads = 0

    def _name(self, i: int) -> bytes:
        return f"d{i:08d}.json".encode()

    def _local(self, i: int) -> bytes:
        return struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 20, 0x800, 0, 0, 0, 0, 0, 0, self.NAME_LEN, 0
        ) + self._name(i)

    def _cd(self, i: int) -> bytes:
        rec = struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014B50, 45, 45, 0x800, 0, 0, 0, 0, 0, 0,
            self.NAME_LEN, 20, 0, 0, 0, 0, 0xFFFFFFFF,
        )  # fmt: skip
        extra = struct.pack("<HHQQ", 0x0001, 16, i * self.local_len, 0)
        return rec + self._name(i) + extra

    def _tail(self) -> bytes:
        rec = struct.pack(
            "<IQHHIIQQQQ",
            0x06064B50,
            44,
            45,
            45,
            0,
            0,
            self.n,
            self.n,
            self.cd_size,
            self.cd_offset,
        )
        loc = struct.pack("<IIQI", 0x07064B50, 0, self.z64_offset, 1)
        eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        return rec + loc + eocd

    async def read(self, offset: int, length: int) -> bytes:
        self.reads += 1
        out = bytearray()
        pos, end = offset, min(offset + length, self.size)
        while pos < end:
            if pos < self.cd_offset:
                i, within = divmod(pos, self.local_len)
                piece = self._local(i)[within:]
            elif pos < self.z64_offset:
                i, within = divmod(pos - self.cd_offset, self.cd_len)
                piece = self._cd(i)[within:]
            else:
                piece = self._tail()[pos - self.z64_offset :]
            piece = piece[: end - pos]
            out += piece
            pos += len(piece)
        return bytes(out)


def zip64_central_directory(data: bytes) -> bytes:
    """Rewrite every central-directory record to carry its sizes and offset in a ZIP64 extra field
    (32-bit fields set to 0xFFFFFFFF), as large real exports do. Adjusts the end records."""
    eocd = data.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack_from("<II", data, eocd + 12)
    n = struct.unpack_from("<H", data, eocd + 10)[0]
    pos, records = cd_off, []
    for _ in range(n):
        fields = list(struct.unpack_from("<IHHHHHHIIIHHHHHII", data, pos))
        nlen, xlen, clen = fields[10], fields[11], fields[12]
        name = data[pos + CD_LEN : pos + CD_LEN + nlen]
        comment = data[pos + CD_LEN + nlen + xlen : pos + CD_LEN + nlen + xlen + clen]
        usize, csize, offset = fields[9], fields[8], fields[16]
        extra = struct.pack("<HHQQQ", 0x0001, 24, usize, csize, offset)
        fields[8] = fields[9] = fields[16] = 0xFFFFFFFF
        fields[11] = len(extra)
        records.append(struct.pack("<IHHHHHHIIIHHHHHII", *fields) + name + extra + comment)
        pos += CD_LEN + nlen + xlen + clen
    cd = b"".join(records)
    z64_off = cd_off + len(cd)
    rec = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, len(cd), cd_off)
    loc = struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return data[:cd_off] + cd + rec + loc + end
