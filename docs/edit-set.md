# Edit sets

An edit set is a JSON file that describes changes to a PDF: page order, form values, text edits, redactions,
annotations, crop, properties and more. Leaf can

- apply one in the window: **File › Import edit set…** shows every change with a checkbox and applies the ones you
  keep as a single step (Ctrl+Z undoes all of it);
- write your pending changes as one: **File › Export edit set…**;
- apply one from the command line: `leaf apply in.pdf changes.json -o out.pdf` (`--dry-run` checks it without
  writing); `leaf edits in.pdf` prints an empty template for a document, `leaf edits --example` a filled-in one.

Applying an edit set uses exactly the same engine as editing by hand, so the file is the same either way. Nothing
the edit set asks for is done blindly: a text edit Leaf can't make faithfully, a line that isn't on the page or a
search without matches is skipped and reported.

## Example

```json
{
  "leaf_edits": 1,
  "pages": [{"page": 2}, {"page": 1, "rotate": 90}, {"blank": [595, 842]}],
  "forms": {"name": "Ada Lovelace", "agree": true},
  "text_edits": [{"page": 2, "find": "Total: 100 EUR", "text": "Total: 120 EUR"}],
  "replace": [{"find": "ACME Ltd", "replace": "ACME GmbH", "pages": "all"}],
  "annotations": [
    {"page": 1, "type": "rect", "rect": [72, 300, 272, 360], "style": {"color": "#d43a2a", "width": 2}},
    {"page": 1, "type": "text", "at": [72, 400], "text": "Approved", "style": {"size": 14, "bold": true}},
    {"page": 1, "type": "note", "at": [500, 72], "contents": "Please check the totals."},
    {"type": "highlight", "search": "deadline"}
  ],
  "redactions": [{"preset": "email"}, {"page": 1, "rect": [72, 700, 300, 720]}],
  "crop": [{"pages": "all", "margins": [10, 10, 10, 10], "unit": "mm"}],
  "metadata": {"title": "Contract (signed copy)", "author": "Legal"},
  "decorations": [
    {"kind": "watermark", "text": "DRAFT"},
    {"kind": "header_footer", "slots": ["", "", "", "", "Page {page} of {pages}", ""]}
  ]
}
```

## Conventions

- **Version**: `"leaf_edits": 1` is required. Unknown sections or keys are errors (so a typo never goes unnoticed).
- **Page numbers** start at 1. Inside `pages`, `page` means a page of the document as the edit set finds it;
  everywhere else it means a page of the result, after `pages` (the same thing when there is no `pages` section).
  Programs holding an open document may write `"uid": <PageRef.uid>` instead of `"page"`.
- **Page ranges** (`"pages"`): a string like `"1-3, 7, 10-"`, `"odd"`, `"even"`, `"all"`, or a list `[1, 2, 5]`.
  Leaving it out means all pages.
- **Coordinates** are PDF points (1/72 inch) with the origin at the top left of the page *as it is displayed with
  its own rotation* (the page's `/Rotate`), not counting rotation or cropping added by the edit set or in Leaf.
  Rectangles are `[x0, y0, x1, y1]`, points `[x, y]`. `leaf text --words` and `leaf annots` print boxes in the same
  space.
- **Binary data** (images, fonts of watermarks, inserted documents) is base64 in `"data"`; edit sets you write by
  hand may use `"file": "path"` instead (relative paths are relative to the edit set file).
- Sections are applied in this order, the same order Leaf uses when saving: pages → forms → text_edits → replace →
  images → annotations → redactions → crop → metadata → decorations → sanitize → ocr.

## Sections

### `pages` — the new page list

A list, in the new order. Each entry is one of

| Entry | Meaning |
|---|---|
| `{"page": n}` | page *n* of the document (list it twice to duplicate it; leave it out to delete it) |
| `{"blank": [w, h]}` | a blank page, size in points |
| `{"file": "other.pdf", "page": k}` | page *k* of another PDF (or an image file) |
| `{"source": "s1", "page": k}` | page *k* of a document embedded in the top-level `"sources": {"s1": {"name": "...", "data": "<base64>"}}` |

Any entry can add `"rotate": 90` (clockwise, multiple of 90, on top of the current rotation).

### `forms` — field values

`{"field name": value}`. Text fields take text, check boxes `true`/`false` (or their on-state), radio groups the
chosen option's export value, dropdowns the option, multi-select list boxes a list. Every widget of that name gets
the value; read-only fields are left alone; unknown names are reported. Signature fields are never touched. To
set a field on some pages only (e.g. a duplicated page whose copy keeps its own value), give
`{"value": ..., "pages": [2]}` or a list of those; exports use this form when copies differ.

### `text_edits` — change the page's own text

```json
{"page": 1, "find": "Invoice date: 1 March", "text": "Invoice date: 3 March"}
```

`find` is the current text of one line, exactly (spacing is ignored); with `"para": true` it is a whole paragraph.
Exports from Leaf identify the text by `"key"` (the original glyphs, stable across copies of the page) and add the
`"original"` text for reading. `leaf edit-text in.pdf --page 1 --list` prints the lines of a page. Leaf refuses
(and reports why) text it can't rewrite faithfully: no reliable character mapping, shaped scripts, vertical
writing, OCR text, outlined or clipping text.

### `replace` — find & replace in the page's own text

```json
{"find": "colour", "replace": "color", "word": true, "case": false, "regex": false, "pages": "1-5"}
```

Every line with a match becomes a text edit (as in Edit › Find & replace). With `"regex": true`, `$1` / `\1` in
`replace` insert groups. Matches across two lines and lines that can't be edited are skipped and reported.

### `images` — images already in the page

```json
{"page": 2, "index": 0, "delete": true}
{"page": 2, "index": 1, "rect": [72, 72, 272, 222], "turns": 1}
{"page": 3, "index": 0, "file": "new-logo.png"}
```

`index` is the image's position among the page's image draws (0-based, content order); exports also carry the
draw's `"key"`. `rect` moves/resizes, `turns` rotates by quarter turns clockwise, `data`/`file` replaces the
picture, `delete` removes that one draw. Inline images can't be edited (reported).

### `annotations` — things drawn on top

Every entry has `"page"`, `"type"`, the geometry for that type, and optionally `"style"`, `"contents"` (comment
text) and `"author"`.

| `type` | Geometry | `style` keys (defaults) |
|---|---|---|
| `rect`, `ellipse` | `rect` | `color` (#d43a2a), `width` (2), `fill` (false), `opacity` (1) |
| `line`, `arrow` | `from`, `to` | `color`, `width`, `opacity` |
| `ink` | `points: [[x, y], ...]` or `strokes: [[[x, y], ...], ...]`; optional `pressure`: pen pressure 0–1 per point (a list per stroke with `strokes`), the line gets thinner/thicker with it | `color` (#1f4fd1), `width` (2), `opacity` |
| `highlight` | `rects: [[x0, y0, x1, y1], ...]`, or `search: "text"` (+ `regex`, `case`, `whole`, `pages`; no `page` needed) | `color` (#ffd83d) |
| `text` | `at: [x, y]` (top left) or `rect` (fixed width); `text` | `font`, `size`, `color`, `bold`, `italic`, `underline`, `strike`, `align`, `spacing`, `bg`, `border`, `pad`, `box_w` |
| `note` | `at: [x, y]`; `contents` | `color` |
| `whiteout` | `rect` | — |
| `image` | `rect`; `data` or `file`; `"signature": true` for a signature | `opacity` |

Shapes, lines, ink, highlights and notes stay real PDF annotations; text boxes, images and whiteouts become page
content when saved (as in Leaf). Whiteout only covers; use `redactions` to remove.

### `redactions` — remove content for good

```json
{"page": 1, "rect": [72, 700, 300, 720]}
{"search": "Jane Doe", "case": true, "whole": true}
{"preset": "email", "pages": "1-3"}
```

Presets: `text` (the default; needs `search`), `regex` (needs `search`), `email`, `phone`, `card` (Luhn-checked),
`iban` (checksum), `date`. Optional `color` (fill, default black) and `label` (text drawn on the box). The text,
image pixels and drawings under each box are removed when the file is saved (a full rewrite).

### `crop`

```json
{"pages": "all", "margins": [10, 10, 10, 10], "unit": "mm"}
{"page": 2, "rect": [36, 36, 559, 806]}
{"pages": "odd", "auto": true, "pad": 6}
{"pages": "all", "remove": true}
```

Margins are left, top, right, bottom as the page is displayed. `auto` trims white margins. Crops set the CropBox
(content outside is hidden, not removed).

### `metadata`

`{"title": "...", "author": "...", "subject": "...", "keywords": "..."}` — the XMP metadata is kept in step.

### `decorations` — watermarks, headers & footers, Bates numbers

Each entry has `"kind"`: `watermark`, `header_footer` or `bates`, plus any of the settings of Document › Watermark
/ Header & footer / Bates numbering: `pages` (`all`, `odd`, `even`, `skip_first`, `range` with `page_range`),
`family` (`sans`, `serif`, `mono`), `bold`, `size`, `color`, `opacity`, `behind`; header & footer: `slots` (six
texts: top left, center, right, bottom left, center, right; tokens `{page}`, `{pages}`, `{label}`, `{date}`,
`{filename}`, `{title}`), `margin_x`, `margin_y`, `start`, `date_format`; Bates: `prefix`, `suffix`, `digits`,
`start`, `slot` (`tl` … `br`); watermark: `text` or `image` (base64), `fit`, `diagonal`, `angle`, `tiled`, `scale`.

### `sanitize`

`{"metadata": true, "javascript": true, "attachments": true, "comments": false, "hidden_text": true,
"hidden_layers": true, "forms": false, "links": false, "thumbnails": false, "page_labels": false}` — what Redact ›
Sanitize removes when saving.

### `ocr` — recognize text

`{"pages": "all", "language": "eng", "engine": "auto", "dpi": 300, "only_if_needed": false}` adds an invisible
text layer so scanned pages can be searched and copied. `engine` is `auto`, `tesseract` or `rapidocr`;
`only_if_needed` skips pages that already have text. Recognition runs when the edit set is applied.

## What an export doesn't include

Exports describe changes relative to the file as it was opened. Not (yet) carried: changes to annotations that were
already in the file (moved, restyled, deleted), new form fields, comment replies, link/bookmark/attachment edits
and password protection. An export lists what it left out under `document.not_exported`, and Import shows it.

## Command line

`leaf <command>` (or `leaf-cli <command>`) runs without a window. Every command that changes a PDF writes a new
file — `-o out.pdf`, or `<name>-<command>.pdf` next to the input — unless `--in-place` is given. `--json` prints a
single JSON object (`ok`, `command`, `code`, and the command's results). Passwords are never taken from the command
line: an encrypted PDF's password comes from `LEAF_PASSWORD` (or the variable named by `--password-env`), a
signing identity's from `LEAF_SIGN_PASSWORD` (or `--identity-password-env`), else Leaf asks on the terminal.

| Command | Does |
|---|---|
| `info`, `text`, `fields`, `annots` | read: summary, text (`--words` for boxes), form fields, annotations |
| `fill` | `leaf fill form.pdf name=Ada agree=true` or `--data values.json` / `.xfdf` |
| `redact` | `--text`, `--regex`, `--preset email`, `--rect 1:72,700,300,720`; `--sanitize`; `--dry-run` |
| `ocr` | `--lang eng --engine auto --pages 1-3 --skip-text` |
| `merge`, `split`, `extract`, `rotate`, `delete`, `reorder` | page operations (`--pages "1-3, 7"`) |
| `crop` | `--rect`, `--margins l,t,r,b --unit mm`, `--auto`, `--remove` |
| `optimize` | `--preset lossless|print|ebook|screen`, `--gray`, `--dpi`, `--quality` |
| `export` | `--format png|jpg|webp|tif` (pages), `images` (embedded), `text`, `markdown` |
| `edit-text` | `--page 2 --find "old line" --text "new line"`; `--list` shows the lines |
| `replace` | `--find --replace [--regex --case --word --pages]` |
| `sign`, `verify` | PAdES signing with a .p12/.pfx; offline verification (`--trust cert.pem`) |
| `apply`, `edits` | apply an edit set; print a template |

Exit codes: 0 done · 1 error · 2 usage (bad arguments, malformed edit set) · 3 password needed or wrong · 4 nothing
done (every change refused, no matches, a signature check failed) · 5 written, but some changes were refused.
