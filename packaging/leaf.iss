; Inno Setup script for the Windows installer. Build dist\Leaf first (packaging\leaf.spec), then:
;   iscc /DVersion=0.2.2 packaging\leaf.iss      -> dist\Leaf-<version>-windows-x64-setup.exe
; OCR variant (built with LEAF_BUNDLE_OCR=1 into dist\ocr\Leaf):
;   iscc /DVersion=0.2.2 /DSourceDir=..\dist\ocr\Leaf /DSuffix=-ocr packaging\leaf.iss
; Per-user install (no admin prompt). Registers Leaf as a PDF app ("Open with", Default apps, Explorer's "Edit with
; Leaf" / "Combine with Leaf") without taking over the default app.

#ifndef Version
  #define Version "0.2.2"
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
DefaultDirName={autopf}\Leaf
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
Name: "sendto"; Description: "Add ""Combine with Leaf"" to Explorer's Send to menu (any number of files)"; GroupDescription: "Additional shortcuts:"

[InstallDelete]
; upgrades (and switching between the standard and OCR variants, same AppId): no stale files from the old bundle
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Leaf"; Filename: "{app}\Leaf.exe"; AppUserModelID: "Leaf.PdfEditor"
Name: "{userdesktop}\Leaf"; Filename: "{app}\Leaf.exe"; Tasks: desktopicon; AppUserModelID: "Leaf.PdfEditor"
; Send to passes every selected file to one process (no 15-file limit): leaf --merge a.pdf b.pdf ...
Name: "{usersendto}\Combine with Leaf"; Filename: "{app}\Leaf.exe"; Parameters: "--merge"; Tasks: sendto

[Registry]
; Leaf as a PDF app (the same entries as leaf/shellint.py registry_entries(); tests/test_shellint.py compares them).
; Nothing takes over the user's default PDF app: Leaf is listed in "Open with", in "How do you want to open this?"
; and in Settings > Apps > Default apps, where the user can choose it.
; Uninstall runs these lines in reverse order: a key removed "if empty" is listed before what goes inside it.
; ProgID: file type name, icon, open command, and the AppUserModelID that gives the taskbar jump list its "Recent"
; files (must match leaf/__main__.py)
Root: HKA; Subkey: "Software\Classes\Leaf.pdf"; ValueType: string; ValueName: ""; ValueData: "PDF document"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Leaf.pdf"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "PDF document"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "Leaf.PdfEditor"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Leaf.exe,0"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Leaf"
Root: HKA; Subkey: "Software\Classes\Leaf.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Leaf.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "Leaf.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\Leaf.exe"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\Leaf.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Leaf"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\Leaf.exe\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey
; Default apps: Capabilities + RegisteredApplications. Software\Leaf also holds Leaf's settings: only removed if empty
Root: HKA; Subkey: "Software\Leaf"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Leaf\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "Leaf"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Leaf\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Edit, sign and redact PDF files on your computer."
Root: HKA; Subkey: "Software\Leaf\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\Leaf.exe,0"
Root: HKA; Subkey: "Software\Leaf\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "Leaf.pdf"
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "Leaf"; ValueData: "Software\Leaf\Capabilities"; Flags: uninsdeletevalue
; Explorer right-click on .pdf (Windows 11: Show more options). One Leaf process per selected file; the running Leaf
; gathers them (leaf/ipc.py). Explorer hides these verbs when more than 15 files are selected.
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.edit"; ValueType: string; ValueName: ""; ValueData: "Edit with &Leaf"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.edit"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\Leaf.exe,0"
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.edit"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Document"
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.edit\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Leaf.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.combine"; ValueType: string; ValueName: ""; ValueData: "Com&bine with Leaf"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.combine"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\Leaf.exe,0"
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.combine"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Document"
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Leaf.combine\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Leaf.exe"" --merge ""%1"""

[Run]
Filename: "{app}\Leaf.exe"; Description: "Launch Leaf"; Flags: nowait postinstall skipifsilent
