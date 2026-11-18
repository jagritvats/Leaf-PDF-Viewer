; Inno Setup script for the Windows installer. Build dist\Leaf first (packaging\leaf.spec), then:
;   iscc /DVersion=0.2.0 packaging\leaf.iss      -> dist\Leaf-<version>-windows-x64-setup.exe
; OCR variant (built with LEAF_BUNDLE_OCR=1 into dist\ocr\Leaf):
;   iscc /DVersion=0.2.0 /DSourceDir=..\dist\ocr\Leaf /DSuffix=-ocr packaging\leaf.iss
; Per-user install (no admin prompt). Registers Leaf under "Open with" for .pdf without taking over the default app.

#ifndef Version
  #define Version "0.2.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Leaf"
#endif
#ifndef Suffix
  #define Suffix ""
#endif

[Setup]
AppId={{6C1E5D2A-4F4B-4E59-9C7B-2B7A1E0F5D11}
AppName=Leaf
AppVersion={#Version}
AppPublisher=Leaf
DefaultDirName={localappdata}\Programs\Leaf
DefaultGroupName=Leaf
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Leaf-{#Version}-windows-x64{#Suffix}-setup
SetupIconFile=..\build\icons\leaf.ico
UninstallDisplayIcon={app}\Leaf.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; upgrades (and switching between the standard and OCR variants, same AppId): no stale files from the old bundle
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Leaf"; Filename: "{app}\Leaf.exe"; AppUserModelID: "Leaf.PdfEditor"
Name: "{userdesktop}\Leaf"; Filename: "{app}\Leaf.exe"; Tasks: desktopicon; AppUserModelID: "Leaf.PdfEditor"

[Registry]
; "Open with" entry for PDFs (does not change the user's default PDF app)
Root: HKA; Subkey: "Software\Classes\Leaf.pdf"; ValueType: string; ValueName: ""; ValueData: "PDF document"; Flags: uninsdeletekey
; jump list "Recent" for files opened through this ProgID: must match the AUMID set in leaf/__main__.py
Root: HKA; Subkey: "Software\Classes\Leaf.pdf"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "Leaf.PdfEditor"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Leaf.exe,0"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Leaf.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "Leaf.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\Leaf.exe\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey

[Run]
Filename: "{app}\Leaf.exe"; Description: "Launch Leaf"; Flags: nowait postinstall skipifsilent
