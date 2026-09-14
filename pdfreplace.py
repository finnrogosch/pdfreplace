"""Replace text in a PDF without re-typesetting the document.

Works on content stream operators rather than raw bytes, so it also finds text the
producer split across elements for kerning: [(Hello W) -20 (orld)] TJ contains
"Hello World". Pages without a match are left byte-for-byte alone.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pikepdf
from pikepdf import Array, ContentStreamInstruction, Name, Operator, Pdf, String

__version__ = "0.1.0"

SHOW_OPS = {"Tj", "TJ", "'", '"'}


class Unsupported(Exception):
    """The text sits in a font we cannot edit safely."""


@dataclass(eq=False)  # identity, not field equality: we look items up by position
class _Item:
    """One piece of a text run: either characters or a kerning number."""

    instr: int
    text: bytes | None = None
    kern: float | None = None
    offset: int = 0  # position in the assembled buffer


@dataclass(eq=False)
class _Run:
    """Contiguous text with no repositioning in between."""

    font: object
    encoding: str
    items: list[_Item] = field(default_factory=list)
    dirty: bool = False

    @property
    def buf(self) -> bytes:
        return b"".join(i.text for i in self.items if i.text is not None)


def _missing_glyphs(font, text: bytes) -> set[bytes]:
    """Characters this font has no glyph for.

    Producers usually embed only the characters a document actually uses (a subset).
    A character that appears nowhere has no glyph and no width, so a viewer stacks it
    invisibly on top of the next one. Writing such a character silently corrupts the page.
    """
    if "/Widths" not in font or "/FirstChar" not in font:
        return set()  # standard font without metrics, the viewer supplies the glyphs
    first, widths = int(font.FirstChar), [float(w) for w in font.Widths]
    missing = set()
    for code in set(text):
        i = code - first
        if not (0 <= i < len(widths)) or widths[i] == 0:
            missing.add(bytes([code]))
    return missing


def _encoding_of(font) -> str:
    if str(font.get("/Subtype", "")) == "/Type0":
        raise Unsupported("Type0 font, holds glyph numbers rather than text")
    enc = font.get("/Encoding")
    if isinstance(enc, Name) and str(enc) == "/WinAnsiEncoding":
        return "cp1252"
    # ponytail: /Differences arrays are ignored, they only deviate outside ASCII.
    # If that bites, this is where per-font encoding tables would go.
    return "latin-1"


def _collect_runs(instrs, resources):
    """Split a content stream into text runs. Returns (runs, skipped_fonts)."""
    fonts = resources.get("/Font", {}) if resources is not None else {}
    runs: list[_Run] = []
    skipped: set[str] = set()
    current: _Run | None = None
    font = encoding = None

    for idx, instr in enumerate(instrs):
        op = str(getattr(instr, "operator", ""))

        if op == "Tf":
            current = None
            font = encoding = None
            name = str(instr.operands[0])
            if name in fonts:
                try:
                    font, encoding = fonts[name], _encoding_of(fonts[name])
                except Unsupported as exc:
                    skipped.add(f"{name}: {exc}")
            continue

        if op not in SHOW_OPS:
            current = None  # anything else may move the cursor, so the run ends here
            continue

        if font is None:
            continue
        if op in ("'", '"') or current is None:
            current = _Run(font, encoding)  # ' and " start a new line
            runs.append(current)

        pos = len(current.buf)
        operand = instr.operands[-1]
        for elem in (operand if op == "TJ" else [operand]):
            if isinstance(elem, String):
                data = bytes(elem)
                current.items.append(_Item(idx, text=data, offset=pos))
                pos += len(data)
            else:
                current.items.append(_Item(idx, kern=float(elem), offset=pos))

        if op in ("'", '"'):
            current = None

    return runs, skipped


def _apply(run: _Run, old: bytes, new: bytes) -> list[str]:
    """Replace old with new throughout the run. Returns one context line per match."""
    buf = run.buf
    hits, start = [], buf.find(old)
    while start != -1:
        hits.append(start)
        start = buf.find(old, start + len(old))
    if not hits:
        return []

    for start in reversed(hits):  # backwards, so recorded offsets stay valid
        end = start + len(old)
        touched = [i for i in run.items if i.text is not None
                   and i.offset < end and i.offset + len(i.text) > start]
        inside = [i for i in run.items if i.kern is not None and start < i.offset < end]

        first, last = touched[0], touched[-1]
        prefix = first.text[: start - first.offset]
        suffix = last.text[end - last.offset:]

        # Kerning numbers inside the match describe gaps between characters that are
        # gone. Carry their sum over so the rest of the line does not shift by the
        # accumulated amount. The width of the replacement is deliberately NOT
        # compensated: wider text should push the line along, not overprint it.
        carry = round(sum(i.kern for i in inside), 2)

        for item in touched:
            item.text = b""
        for item in inside:
            item.kern = None
        first.text = prefix + new

        # Carry and remainder belong right behind the new text, not at the run's end.
        after = []
        if carry:
            after.append(_Item(first.instr, kern=carry, offset=end))
        if suffix:
            after.append(_Item(last.instr, text=suffix, offset=end))
        pos = run.items.index(first) + 1
        run.items[pos:pos] = after

    lines = []
    for start in hits:
        lo = max(0, start - 40)
        snippet = buf[lo: start + len(old) + 40].decode(run.encoding, errors="replace")
        cut = start - lo
        marked = f"{snippet[:cut]}«{snippet[cut:cut + len(old)]}»{snippet[cut + len(old):]}"
        lines.append(" ".join(marked.split()))
    return lines


def _rebuild(instrs, runs) -> list:
    """Reassemble the show operators we changed; pass everything else through."""
    changed: dict[int, list[_Item]] = {}
    for run in runs:
        if run.dirty:
            for item in run.items:
                changed.setdefault(item.instr, []).append(item)

    out = []
    for idx, instr in enumerate(instrs):
        if idx not in changed:
            out.append(instr)
            continue
        elements = []
        for item in changed[idx]:
            if item.kern is not None:
                elements.append(item.kern)
            elif item.text:
                elements.append(String(item.text))
        op = str(instr.operator)
        if op == '"':  # word and character spacing are side effects of "
            out.append(ContentStreamInstruction([instr.operands[0]], Operator("Tw")))
            out.append(ContentStreamInstruction([instr.operands[1]], Operator("Tc")))
        if op in ("'", '"'):  # both advance to the next line before showing
            out.append(ContentStreamInstruction([], Operator("T*")))
        out.append(ContentStreamInstruction([Array(elements)], Operator("TJ")))
    return out


def replace(pdf: Pdf, old: str, new: str):
    """Replace old with new on every page of an open Pdf.

    Returns (matches, skipped, blocked). Each match is a (page number, context) pair.
    `skipped` names text that was left alone, `blocked` holds characters the document's
    fonts cannot render; if it is non-empty, nothing was modified.
    """
    found, skipped, blocked = [], set(), set()
    for pageno, page in enumerate(pdf.pages, 1):
        if "/Contents" not in page:
            continue
        instrs = list(pikepdf.parse_content_stream(page))
        runs, page_skipped = _collect_runs(instrs, page.get("/Resources"))
        skipped |= page_skipped

        touched = False
        for run in runs:
            try:
                needle, repl = old.encode(run.encoding), new.encode(run.encoding)
            except UnicodeEncodeError:
                skipped.add(f"replacement not representable in {run.encoding}")
                continue
            if needle not in run.buf:
                continue
            gaps = _missing_glyphs(run.font, repl)
            if gaps:
                blocked |= {g.decode(run.encoding, errors="replace") for g in gaps}
                continue
            hits = _apply(run, needle, repl)
            if hits:
                run.dirty = touched = True
                found += [(pageno, line) for line in hits]
        if touched:  # pages without a match keep their original stream
            page.Contents = pdf.make_stream(
                pikepdf.unparse_content_stream(_rebuild(instrs, runs)))
    return found, skipped, blocked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdfreplace",
        description="Replace a string in a PDF. Without -o it only previews.")
    ap.add_argument("pdf")
    ap.add_argument("old", help="text to look for")
    ap.add_argument("new", help="text to put in its place")
    ap.add_argument("-o", "--output", help="file to write; omit it and nothing is written")
    ap.add_argument("--password", default="", help="password for protected PDFs")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    try:
        pdf = Pdf.open(args.pdf, password=args.password)
    except pikepdf.PasswordError:
        print("PDF is password protected, pass --password.", file=sys.stderr)
        return 2
    except pikepdf.PdfError as exc:
        print(f"Cannot open {args.pdf}: {exc}", file=sys.stderr)
        return 2

    with pdf:
        acroform = pdf.Root.get("/AcroForm")
        if acroform is not None and int(acroform.get("/SigFlags", 0)) & 1:
            print("Note: this PDF is digitally signed. Any edit invalidates "
                  "the signature.\n", file=sys.stderr)

        found, skipped, blocked = replace(pdf, args.old, args.new)

        if blocked:
            print("Stopped: this PDF embeds its font as a subset. It has no glyph for "
                  f"{' '.join(repr(c) for c in sorted(blocked))}, so those characters "
                  "would stack up invisibly.\nPick a replacement built from characters "
                  "the document already uses.", file=sys.stderr)
            return 2
        if skipped:
            print("Left untouched:\n  " + "\n  ".join(sorted(skipped)), file=sys.stderr)
        for pageno, line in found:
            print(f"p.{pageno}  {line}")

        if not found:
            print(f"\n'{args.old}' not found.", file=sys.stderr)
            if not skipped:
                print("It may live in a form XObject or a form field; pdfreplace does "
                      "not edit those yet.", file=sys.stderr)
            return 1

        if not args.output:
            print(f"\n{len(found)} match(es). Preview only, nothing written. "
                  "To apply: -o OUTPUT.pdf")
            return 0

        if len(args.new) != len(args.old):
            print("\nNote: the replacement has a different length, so the rest of the "
                  "line shifts. A PDF does not re-wrap lines.", file=sys.stderr)
        pdf.save(args.output)
        print(f"\n{len(found)} replacement(s) -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
