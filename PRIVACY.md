# Privacy policy

*Last updated: 18 November 2026*

Leaf is a PDF editor that works on your computer. This policy covers the Leaf desktop app, however it was installed
(Microsoft Store, GitHub releases, Linux packages or from source).

## What Leaf collects

**Nothing.** Leaf has no accounts, analytics, telemetry, crash reporting or ads. Your documents are opened, edited
and saved only on your computer, and are never uploaded anywhere.

## What stays on your computer

- Your settings and the list of recently opened files. Previews of those files are made while Leaf runs and are not
  saved.
- Recovery copies of unsaved work, so it can be restored after a crash. They are deleted when you save or close the
  document.
- If you turn on the agent link (off by default), a random access token in your user profile that only you can read.

All of this is stored in your user profile. Uninstalling Leaf, or deleting its settings folder, removes it.

## When Leaf uses the network

Leaf connects to the internet only when you ask for something that needs it:

- **Downloading OCR languages.** When you choose *Add languages…* in the OCR dialog, Leaf downloads the language files
  you pick from the Tesseract project on GitHub (`github.com/tesseract-ocr/tessdata_fast`). GitHub sees a normal
  download request (your IP address and the file name). No document content is sent.
- **Timestamped signatures.** If you enter a timestamp server when signing, Leaf sends that server a hash of the
  signature, as the PAdES standard requires. The server never sees the document itself.
- **Links in documents.** Clicking a web link in a PDF opens it in your web browser.

Leaf's single-instance handover and agent link use local connections inside your computer, not the network.

## Children

Leaf collects no personal information from anyone, including children.

## Changes

Changes to this policy are published in this file, in Leaf's public repository:
<https://github.com/jagritvats/Leaf-PDF-Viewer/blob/main/PRIVACY.md>.

## Contact

Questions: open an issue at <https://github.com/jagritvats/Leaf-PDF-Viewer/issues>.
