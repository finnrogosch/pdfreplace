"""Run with: uv run --with pikepdf --with pytest pytest"""

import pikepdf
from pikepdf import Dictionary, Name, Pdf

import pdfreplace


def build(content: bytes, widths=True, type0=False, subset="") -> Pdf:
    """A one-page PDF with a single content stream and one font."""
    pdf = Pdf.new()
    font = Dictionary(Type=Name.Font, BaseFont=Name.Helvetica,
                      Subtype=Name.Type0 if type0 else Name.Type1,
                      Encoding=Name.WinAnsiEncoding)
    if widths:
        font.FirstChar = 32
        font.Widths = [500] * 224  # uniform, which keeps the arithmetic checkable
        for char in subset:  # width 0 means the glyph is not embedded
            font.Widths[ord(char) - 32] = 0
    page = pdf.add_blank_page(page_size=(595, 842))
    page.Contents = pdf.make_stream(content)
    page.Resources = Dictionary(Font=Dictionary(F1=pdf.make_indirect(font)))
    return pdf


def text_of(pdf, pageno=0) -> bytes:
    out = b""
    for instr in pikepdf.parse_content_stream(pdf.pages[pageno]):
        if str(instr.operator) in pdfreplace.SHOW_OPS:
            operand = instr.operands[-1]
            for elem in (operand if str(instr.operator) == "TJ" else [operand]):
                if isinstance(elem, pikepdf.String):
                    out += bytes(elem)
    return out


def elements_of(pdf, pageno=0):
    """The TJ array flattened into bytes and numbers, empty strings dropped."""
    out = []
    for instr in pikepdf.parse_content_stream(pdf.pages[pageno]):
        if str(instr.operator) == "TJ":
            out += [bytes(e) if isinstance(e, pikepdf.String) else float(e)
                    for e in instr.operands[0]]
    return [e for e in out if e != b""]


def test_finds_a_match_split_by_kerning():
    """The case plain byte replacement in the raw stream misses."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td [(Invoice W) -20 (orld 2025) 15 (!)] TJ ET")
    found, skipped, _ = pdfreplace.replace(pdf, "World 2025", "Earth 2026")
    assert len(found) == 1 and not skipped
    assert text_of(pdf) == b"Invoice Earth 2026!"


def test_finds_a_match_across_show_operators():
    pdf = build(b"BT /F1 12 Tf 72 700 Td (part A) Tj (BC end) Tj ET")
    found, _, _ = pdfreplace.replace(pdf, "ABC", "XYZ")
    assert len(found) == 1
    assert text_of(pdf) == b"part XYZ end"


def test_repositioning_separates_runs():
    """Text at two different Td positions does not belong together."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td (part A) Tj 72 680 Td (BC end) Tj ET")
    found, _, _ = pdfreplace.replace(pdf, "ABC", "XYZ")
    assert found == []


def test_replaces_every_match_in_a_run():
    pdf = build(b"BT /F1 12 Tf 72 700 Td (2025 until 2025) Tj ET")
    found, _, _ = pdfreplace.replace(pdf, "2025", "2026")
    assert len(found) == 2
    assert text_of(pdf) == b"2026 until 2026"


def test_width_is_not_compensated():
    """Wider replacements should push the line along, not overprint it."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td (price 99 EUR) Tj ET")
    pdfreplace.replace(pdf, "99", "1000")
    assert elements_of(pdf) == [b"price 1000", b" EUR"]


def test_kerning_inside_a_match_is_carried_over():
    """That number describes a gap between characters that no longer exist."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td [(20) 30 (25 EUR)] TJ ET")
    pdfreplace.replace(pdf, "2025", "2026")
    assert elements_of(pdf) == [b"2026", 30.0, b" EUR"]


def test_carry_sits_before_the_remaining_text():
    """Otherwise the kerning pushes the rest of the line into the new text."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td [(price 10) 25 (00 EUR)] TJ ET")
    pdfreplace.replace(pdf, "1000", "99")
    assert elements_of(pdf) == [b"price 99", 25.0, b" EUR"]


def test_kerning_outside_the_match_survives():
    pdf = build(b"BT /F1 12 Tf 72 700 Td [(price) 40 (1000) 50 (EUR)] TJ ET")
    pdfreplace.replace(pdf, "1000", "99")
    assert elements_of(pdf) == [b"price", 40.0, b"99", 50.0, b"EUR"]


def test_type0_is_refused_rather_than_corrupted():
    pdf = build(b"BT /F1 12 Tf 72 700 Td (2025) Tj ET", type0=True)
    found, skipped, _ = pdfreplace.replace(pdf, "2025", "2026")
    assert found == [] and skipped
    assert text_of(pdf) == b"2025"


def test_missing_glyphs_block_the_replacement():
    """Subset fonts: never write a character the font does not carry."""
    pdf = build(b"BT /F1 12 Tf 72 700 Td (price 99) Tj ET", subset="PRICE")
    found, _, blocked = pdfreplace.replace(pdf, "99", "PRICE")
    assert blocked == set("PRICE")
    assert found == [] and text_of(pdf) == b"price 99"


def test_page_without_a_match_is_left_alone():
    pdf = build(b"BT /F1 12 Tf 72 700 Td (2025) Tj ET")
    pdf.add_blank_page(page_size=(595, 842))
    pdf.pages[1].Contents = pdf.make_stream(b"BT /F1 12 Tf 72 700 Td (nothing) Tj ET")
    pdf.pages[1].Resources = pdf.pages[0].Resources
    before = bytes(pdf.pages[1].Contents.read_raw_bytes())
    pdfreplace.replace(pdf, "2025", "2026")
    assert bytes(pdf.pages[1].Contents.read_raw_bytes()) == before


def test_result_is_a_readable_pdf(tmp_path):
    pdf = build(b"BT /F1 12 Tf 72 700 Td [(Hello W) -20 (orld 2025)] TJ ET")
    pdfreplace.replace(pdf, "2025", "2026")
    out = tmp_path / "out.pdf"
    pdf.save(out)
    with Pdf.open(out) as again:
        assert text_of(again) == b"Hello World 2026"


def test_dry_run_writes_nothing(tmp_path, capsys):
    source = tmp_path / "in.pdf"
    build(b"BT /F1 12 Tf 72 700 Td (2025) Tj ET").save(source)
    before = source.read_bytes()
    assert pdfreplace.main([str(source), "2025", "2026"]) == 0
    assert source.read_bytes() == before
    assert "nothing written" in capsys.readouterr().out


def test_exit_code_1_when_nothing_matches(tmp_path):
    source = tmp_path / "in.pdf"
    build(b"BT /F1 12 Tf 72 700 Td (2025) Tj ET").save(source)
    assert pdfreplace.main([str(source), "missing", "x"]) == 1


def test_cli_refuses_to_write_missing_glyphs(tmp_path):
    source = tmp_path / "in.pdf"
    build(b"BT /F1 12 Tf 72 700 Td (price 99) Tj ET", subset="PRICE").save(source)
    assert pdfreplace.main([str(source), "99", "PRICE", "-o", str(tmp_path / "out.pdf")]) == 2
    assert not (tmp_path / "out.pdf").exists()
