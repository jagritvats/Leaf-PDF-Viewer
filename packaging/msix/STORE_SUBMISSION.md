# Microsoft Store submission

Checklist and ready-to-paste text for publishing Leaf in the Microsoft Store as an MSIX. The Store signs the
package itself, so no code-signing certificate is needed.

## 1. One-time setup (account owner)

1. Create a developer account in [Partner Center](https://partner.microsoft.com/dashboard/registration). Choose an
   individual or company account; identity verification can take a few days.
2. **Apps and games › New product › MSIX or PWA app**, then reserve the name. "Leaf" is probably taken, so try
   **Leaf PDF Editor**.
3. Open the product, then **Product management › Product identity**, and copy three values:
   - `Package/Identity/Name`, e.g. `12345Name.LeafPDFEditor`
   - `Package/Identity/Publisher`, e.g. `CN=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX`
   - `Package/Properties/PublisherDisplayName`

## 2. Build the package

From the repo root, after building `dist\Leaf` with `packaging\leaf.spec` (Windows 10/11 SDK required):

```powershell
pwsh packaging\msix\build_msix.ps1 -IdentityName "<Name>" -Publisher "CN=<...>" `
     -PublisherDisplayName "<PublisherDisplayName>" -DisplayName "<reserved name>" -NoCli
```

This writes `dist\Leaf-<version>-windows-x64.msix`. `-DisplayName` must be exactly the reserved name. Each new
submission needs a higher version: bump `pyproject.toml`; the fourth part of the version is always 0.

## 3. Submission

**Pricing and availability:** free, all markets (or your choice).

**Properties**
- Category: *Productivity*. Subcategory: none.
- Privacy policy URL: `https://github.com/jagritvats/Leaf-PDF-Viewer/blob/main/PRIVACY.md`
- Website: `https://github.com/jagritvats/Leaf-PDF-Viewer`
- Support contact: `https://github.com/jagritvats/Leaf-PDF-Viewer/issues`
- Product declarations: tick *This app has been tested to meet accessibility guidelines* only once you have
  checked it with Narrator yourself.

**Age ratings (IARC questionnaire):** category *Productivity / utility*. Answer **No** to violence, sexual content,
gambling, user interaction/chat, sharing location and purchases. That gives the lowest rating (3+ / Everyone).

**Packages:** upload the `.msix` built in step 2.

**Restricted capability `runFullTrust`.** Paste this justification:

> Leaf is a desktop PDF editor (Win32, Qt) packaged as MSIX. runFullTrust is required because the app is a
> classic Win32 desktop application. It reads and writes PDF files the user opens or saves through the standard
> file dialogs, and runs its rendering and OCR in worker processes of its own executable. It installs no drivers
> or services, and makes no network requests except optional downloads the user starts (OCR language data) and
> an optional signature timestamp server the user enters.

**Store listing (English)**
- Description: the *Store: full description* in the promo copy deck (`marketing/promo/make_promo.py` writes it to
  `marketing/out/promo/COPY.md`). Summarize the release notes in *What's new in this version*.
- Short description: the *Store: short description* from the same file.
- Screenshots: 1920×1080 PNGs, up to 10. Use `marketing/shots/shoot_all.py` to render them from fictional demo
  documents only.
- Keywords: up to 7. Suggested: PDF editor, edit PDF, redact PDF, sign PDF, fill PDF form, OCR, offline.
- Store logos: optional. The package already carries its tile icons.

## 4. After certification

Certification usually takes 1–3 business days. Each later release repeats steps 2 and 3 with a new version.
