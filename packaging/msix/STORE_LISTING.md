# Microsoft Store: fields to fill in

Partner Center, product **Leaf PDF Editor** (created with **New product › MSIX or PWA app**). Each section below
matches a page of the submission. Texts are ready to paste.

---

## 1. Product identity (Product management › Product identity)

Copy these three values. The package is built with them (`packaging\msix\build_msix.ps1`):

| Field | Used as |
|---|---|
| Package/Identity/Name | `-IdentityName` |
| Package/Identity/Publisher (`CN=…`) | `-Publisher` |
| Package/Properties/PublisherDisplayName | `-PublisherDisplayName` |

The display name is fixed: `-DisplayName "Leaf PDF Editor"`.

---

## 2. Pricing and availability

- **Markets:** all markets (default)
- **Visibility:** Public audience. Discoverable: *Make this product available and discoverable in the Store*
- **Schedule:** as soon as possible
- **Pricing:** Free
- **Free trial:** No free trial

---

## 3. Properties

- **Category:** Productivity
- **Subcategory:** none
- **Privacy policy URL:** `https://github.com/jagritvats/Leaf-PDF-Viewer/blob/main/PRIVACY.md`
- **Website:** `https://github.com/jagritvats/Leaf-PDF-Viewer`
- **Support contact info:** `https://github.com/jagritvats/Leaf-PDF-Viewer/issues`
- **Game settings:** not applicable
- **Display mode:** leave unchecked
- **Product declarations:**
  - ☐ *This app has been tested to meet accessibility guidelines*. Tick it only after you have tried it with
    Narrator yourself.
  - ☑ *Customers can install this app to alternate drives or removable storage*
  - ☑ *Windows can include this app's data in automatic backups to OneDrive*
  - ☐ *This app depends on non-Microsoft drivers or NT services*
- **System requirements:**
  - Minimum: Memory 4 GB; DirectX: none; Video memory: none.
  - Recommended: Memory 8 GB.
  - Input hardware: Mouse, Keyboard. Tick Pen as recommended, not required: Leaf supports pen pressure and the
    eraser end.
  - Architecture: x64.

---

## 4. Age ratings (IARC questionnaire)

- **Category:** *Productivity / Utility / Communication or other app*. Pick the one that isn't a game and isn't a
  social or communication app.
- Answer **No** to every question:
  - Does the app allow users to interact or exchange content? → **No**
  - Does the app share the user's current physical location? → **No**
  - Does the app allow purchases of digital goods? → **No**
  - Is it a web browser or search engine? → **No**
  - Does it contain or promote violence, sexual content, gambling, drugs, crude language or hate? → **No**
- Expected result: the lowest rating (IARC 3+ / Everyone).

---

## 5. Packages

Upload `Leaf-0.2.0-windows-x64.msix`. The build needs the identity values from section 1. Device family: Desktop
(set by the package).

---

## 6. Store listings › English (United States)

### Product name
Leaf PDF Editor

### Description
```
Leaf is a fast, native PDF editor that works entirely on your computer. Your documents are never uploaded anywhere.

• Edit the text that's already there. Click a line and type; Leaf keeps the document's own font, size and color.
• True redaction. Text, images and drawings under each mark are removed from the saved file, not just covered.
• Fill & sign forms. Type into fields, tick boxes and pick options, then add a signature you drew, typed or imported.
• Digital signatures. PAdES signatures with your own .p12/.pfx certificate or one created in Leaf, verified offline.
• OCR built in. Make scanned pages searchable and selectable. English is included; more languages can be downloaded.
• Pages, your way. Reorder, rotate, merge, split and crop pages, and insert pages from other PDFs.
• Annotate. Highlights, notes with replies, shapes, freehand ink with pen pressure and text boxes.
• And more: reduce file size, compare two PDFs, find and replace, watermarks, headers and page numbers, print and export.

Leaf follows Windows 11: light and dark themes, your accent color, and Focus mode for distraction-free reading.

No account, no ads, no tracking. Leaf only goes online when you ask it to, for example to download an extra OCR language.

Leaf is free and open source under the GNU AGPL v3.
```

### What's new in this version
```
A new look that follows Windows 11 and your accent color, a start screen with your recent files as page previews, Focus mode (Ctrl+Shift+F) for distraction-free reading, and a fix for pages that could appear late after opening a document.
```

### Product features (one per line, up to 20)
```
Edit existing text in the document's own font
True redaction that removes content from the file
Fill in and sign PDF forms
PAdES digital signatures, verified offline
Built-in OCR to make scans searchable
Reorder, rotate, merge, split and crop pages
Highlights, notes, shapes, ink and text boxes
Reduce file size
Compare two PDFs
Light and dark themes that follow Windows
Focus mode for distraction-free reading
Works offline: documents never leave your PC
```

### Screenshots (Desktop)
Upload in this order (1920×1080 PNG, from `marketing/out/screens/dark` or `light`). Use one theme, or mix dark and
light:
1. `01-hero-annotate-1920x1080.png`: caption *Annotate and discuss*
2. `03-edit-text-1920x1080.png`: *Edit the text that's already there*
3. `05-redact-1920x1080.png`: *Redaction that really removes*
4. `02-forms-1920x1080.png`: *Fill and sign forms*
5. `06-digital-signature-1920x1080.png`: *Digital signatures*
6. `07-ocr-1920x1080.png`: *Make scans searchable*
7. `04-organize-pages-1920x1080.png`: *Organize pages*
8. `11-start-screen-1920x1080.png`: *Pick up where you left off*
9. `12-focus-mode-1920x1080.png`: *Focus mode*
10. `10-night-mode-1920x1080.png`: *Comfortable at night*

### Store logos
Optional; the package already has its icons. If you want one: 1:1 box art `marketing/out/promo/tile-dark-2160x2160.png`.

### Search terms (up to 7)
```
PDF editor
edit PDF
redact PDF
sign PDF
fill PDF form
OCR
offline PDF
```

### Copyright and trademark info
```
© 2026 Jagrit Vats. Licensed under the GNU AGPL v3.0 or later.
```

### Additional license terms
```
Leaf is free software under the GNU Affero General Public License v3.0 or later: https://github.com/jagritvats/Leaf-PDF-Viewer/blob/main/LICENSE. Source code: https://github.com/jagritvats/Leaf-PDF-Viewer
```

### Developed by
```
Synpse Labs
```

Leave the other fields empty: short title, sort title, voice title, short description (that one is for games), and
trailers.

---

## 7. Submission options

### Restricted capabilities: runFullTrust (justification)
```
Leaf is a desktop PDF editor (Win32, Qt) packaged as MSIX. runFullTrust is required because it is a classic Win32 desktop application. It reads and writes PDF files that the user opens or saves through the standard file dialogs, and runs its page rendering and OCR in worker processes of its own executable. It installs no drivers or services. It makes no network requests except optional downloads the user starts (OCR language data) and an optional signature timestamp server that the user enters.
```

### Notes for certification
```
No account or sign-in is needed. To test: launch Leaf PDF Editor, then File > Open any PDF. Try editing text (Edit text tool), annotating (toolbar), filling a form, and File > Save As. The app works fully offline.
```

### Publishing hold
*Publish this submission as soon as it passes certification*, or choose a date.
