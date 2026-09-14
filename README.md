# pdfreplace

Replace a string inside a PDF, in place, without re-typesetting the document.

```console
$ uvx pdfreplace invoice.pdf "2025" "2026"
p.1  Invoice date: 01 January «2025»
p.3  Valid through December «2025»

2 match(es). Preview only, nothing written. To apply: -o OUTPUT.pdf

$ uvx pdfreplace invoice.pdf "2025" "2026" -o fixed.pdf
2 replacement(s) -> fixed.pdf
```

There is no separate search command: without `-o` it is a dry run. Nothing is written
until you name an output file.

## Install

```console
uvx pdfreplace ...      # no install at all
pipx install pdfreplace # or keep it around
pip install pdfreplace
```

The only dependency is [pikepdf](https://github.com/pikepdf/pikepdf), whose wheels ship
libqpdf. No system packages to install.

## Why not just edit the raw stream

The obvious approach is `qpdf --qdf`, a `sed`, and `fix-qdf`. It works on the PDF a
tutorial shows you and fails on most real ones, because producers split text across
elements to apply kerning:

```
[(Hello W) -20 (orld)] TJ
```

A byte search for `Hello World` finds nothing there. pdfreplace parses the content
stream into operators, joins each run of text back together, searches that, and
distributes the replacement across the original elements. Kerning numbers inside the
match are folded into a single carry value so the rest of the line does not drift.

## What it will refuse to do

Silent corruption is worse than an error, so pdfreplace stops rather than guess:

- **Subset fonts.** Producers embed only the characters a document actually uses. If you
  ask for a character that has no glyph, a viewer stacks it invisibly on top of the next
  one and the page quietly turns to mush. pdfreplace checks the font's widths first and
  refuses, naming the characters it cannot render.
- **Type0 / Identity-H fonts.** There the bytes are glyph numbers, not text. Reading them
  back requires the ToUnicode map and writing them requires glyphs that are usually
  absent from the subset. Those runs are reported and left untouched.
- **Digitally signed PDFs** are edited if you insist, but you get told that any change
  invalidates the signature.

Pages without a match keep their original content stream byte for byte.

## Known limits

- Text inside form XObjects and AcroForm field values is not searched yet.
- A replacement of a different length shifts the rest of the line. A PDF stores final
  positions, so nothing re-wraps; keep lengths close if the layout is tight.
- `/Differences` encodings are read as Latin-1, which is right for ASCII and can be
  wrong for unusual characters above it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | matches found (and written, if `-o` was given) |
| 1 | no match |
| 2 | could not open the file, or refused to write |

## Development

```console
uv run --with pikepdf --with pytest pytest
```

MIT licensed.
