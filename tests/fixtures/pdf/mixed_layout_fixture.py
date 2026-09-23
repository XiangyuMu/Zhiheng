from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter


def build_mixed_layout_pdf() -> bytes:
    """Build the byte-stable three-page fixture used by parser contract tests."""

    writer = PdfWriter()
    first = writer.add_blank_page(width=612, height=792)
    second = writer.add_blank_page(width=612, height=792)
    third = writer.add_blank_page(width=612, height=792)
    third.rotate(90)
    third.cropbox.lower_left = (18, 24)
    third.cropbox.upper_right = (594, 768)
    writer.add_metadata(
        {
            "/Title": "Zhiheng mixed-layout fixture",
            "/Author": "Zhiheng tests",
            "/Subject": "text scan table image rotation",
        }
    )
    # Keep references alive so future pypdf versions cannot optimize pages away.
    assert first.mediabox.width == second.mediabox.width == 612
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
