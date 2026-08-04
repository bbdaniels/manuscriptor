"""A PDF small enough to read and real enough for pdfminer, built by hand.

The pagination gate has to be tested on a machine with no TeX at all, and it
has to be tested on footers that are WRONG -- which no working LaTeX run will
ever produce on demand. So the fixture is the PDF itself: a page tree, a
content stream per page, and one string placed at a chosen height above the
bottom edge, which is the only property the gate reads.
"""
from __future__ import annotations

from pathlib import Path


def _pdf(path: Path, pages: list[str | None], *, y: float = 32.0,
         height: float = 792.0, width: float = 612.0) -> Path:
    """A PDF of `len(pages)` pages, each carrying its string at `y` from the
    bottom, or nothing at all when the entry is None."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"")  # 1: catalog, filled in below
    add(b"")  # 2: page tree
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids = []
    for text in pages:
        if text is None:
            stream = b""
        else:
            stream = (f"BT /F1 10 Tf 260 {y} Td ({text}) Tj ET").encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (int(width), int(height), font, content)
        )
        kids.append(page)

    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = (b"<< /Type /Pages /Count %d /Kids [%s] >>"
               % (len(kids), b" ".join(b"%d 0 R" % k for k in kids)))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, start))
    path.write_bytes(bytes(out))
    return path


def _two_lines(path: Path, *, top: str, bottom: str) -> Path:
    """One page carrying two strings, one in the body and one in the footer."""
    stream = (f"BT /F1 10 Tf 260 400 Td ({top}) Tj ET "
              f"BT /F1 10 Tf 260 32 Td ({bottom}) Tj ET").encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [5 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, start))
    path.write_bytes(bytes(out))
    return path
