"""What Leaf hands to the operating system to open: links in a document and its attached files.

QDesktopServices.openUrl is ShellExecute on Windows (it runs programs and scripts, and reaches out to network
shares, sending the user's NTLM credentials) and xdg-open/gio elsewhere. A document can't be trusted, so:

- web and mail links open after the usual "Open this link outside Leaf?" question;
- links to programs, scripts, shortcuts and other files that run code are refused, as are network paths
  (\\\\server\\share), links to files that don't exist and schemes known to start other programs (javascript:,
  ms-msdt:, search-ms:, ...);
- anything else unusual (another app's URL scheme, a file Leaf doesn't know as a document) asks with a warning and
  Cancel as the default button.

Attached files are checked by the name they are written under (`safe_filename`), so trailing dots or control
characters can't hide a ".exe" from the check, and are marked as coming from the internet on Windows
(`mark_untrusted`), so SmartScreen and Office's Protected View treat them like a downloaded file.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass

# opening these runs code (or a script host, an installer, a shortcut to anything)
RISKY = {".exe", ".com", ".bat", ".cmd", ".msi", ".msp", ".mst", ".scr", ".pif", ".cpl", ".dll", ".ocx", ".sys",
         ".drv", ".jar", ".jnlp", ".js", ".jse", ".vb", ".vbs", ".vbe", ".wsf", ".wsh", ".wsc", ".ws", ".sct",
         ".ps1", ".psm1", ".psd1", ".ps1xml", ".ps2", ".ps2xml", ".psc1", ".psc2", ".pssc", ".msh", ".msh1",
         ".msh2", ".mshxml", ".msh1xml", ".msh2xml", ".hta", ".chm", ".hlp", ".lnk", ".url", ".website", ".scf",
         ".inf", ".ins", ".isp", ".reg", ".msc", ".job", ".shb", ".shs", ".gadget", ".application", ".appref-ms",
         ".xbap", ".settingcontent-ms", ".library-ms", ".search-ms", ".searchconnector-ms", ".diagcab", ".cab",
         ".appx", ".appxbundle", ".msix", ".msixbundle", ".xll", ".xla", ".xlam", ".xlm", ".docm", ".dotm", ".xlsm",
         ".xltm", ".pptm", ".potm", ".ppam", ".ppsm", ".sldm", ".mde", ".accde", ".ade", ".adp", ".mam", ".iso",
         ".img", ".vhd", ".vhdx", ".sh", ".bash", ".zsh", ".csh", ".ksh", ".fish", ".command", ".tool", ".app",
         ".py", ".pyw", ".pyz", ".pyzw", ".pl", ".rb", ".php", ".lua", ".tcl", ".appimage", ".run", ".bin", ".out",
         ".elf", ".deb", ".rpm", ".snap", ".flatpak", ".flatpakref", ".flatpakrepo", ".desktop", ".kdelnk",
         ".dmg", ".pkg", ".mpkg", ".workflow", ".action", ".scpt", ".apk", ".ipa"}
# opened after the usual question; any other file asks with a warning (Cancel by default)
DOCUMENTS = {".pdf", ".txt", ".md", ".csv", ".tsv", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
             ".odg", ".epub", ".xps", ".oxps", ".djvu", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
             ".webp", ".heic", ".jxr", ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".mp4", ".m4v", ".mov", ".webm",
             ".mkv", ".avi"}
WEB_SCHEMES = {"http", "https", "mailto"}
# schemes that start programs, reach network shares or run script: never opened from a document
# (and every "ms-..." scheme: ms-msdt, ms-officecmd, ms-settings, ...)
BLOCKED_SCHEMES = {"javascript", "vbscript", "data", "jar", "shell", "search", "search-ms", "mk", "its", "mhtml",
                   "hcp", "res", "smb", "cifs", "nfs", "afp", "webdav", "dav", "davs", "ssh", "sftp", "telnet", "ldap",
                   "ldaps", "appinstaller", "steam", "vscode", "vscode-insiders"}
_BIDI_AND_CONTROL = ("Cc", "Cf")


@dataclass(frozen=True)
class Verdict:
    action: str  # "open": ask as usual; "warn": ask with `message`, Cancel by default; "block": refuse (`message`)
    target: str  # what to open: a URL, or a local file path
    message: str = ""
    local: bool = False  # `target` is a local path (else a URL)


def shown(text: str) -> str:
    """`text` for a confirmation dialog: control and bidi/format characters (which can make "exe.pdf" read
    "fdp.exe") are shown as escapes."""
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in _BIDI_AND_CONTROL else c for c in text)


def safe_filename(name: str) -> str:
    """The name an attached file is written under: its last path part, without characters Windows doesn't allow
    in names, control or bidi characters, and trailing dots or spaces (which Windows drops: "x.exe." *is*
    "x.exe")."""
    base = os.path.basename(str(name).replace("\\", "/"))
    s = "".join(c for c in base if c not in '<>:"/\\|?*' and unicodedata.category(c) not in _BIDI_AND_CONTROL)
    return s.strip(" .") or "attachment"


def is_risky(name: str) -> bool:
    """Whether opening a file of this name may run code. Judged by the name the shell would see."""
    base = os.path.basename(str(name).replace("\\", "/"))
    s = "".join(c for c in base if unicodedata.category(c) not in _BIDI_AND_CONTROL).rstrip(" .").lower()
    if sys.platform == "win32" and ":" in s:  # NTFS stream syntax ("x.exe::$DATA"): runs as the named file
        return True
    return os.path.splitext(s)[1] in RISKY


def is_network_path(p: str) -> bool:
    """A UNC/device path (\\\\server\\share, \\\\?\\UNC\\..., \\/server/share, \\??\\UNC\\...) on Windows,
    a path starting with // elsewhere."""
    p = str(p).strip()
    if sys.platform == "win32":
        s = p.replace("/", "\\")
        return s.startswith("\\\\") or s.startswith("\\??\\")
    return p.startswith("//")


def mark_untrusted(path: str) -> None:
    """Windows: give a file written from a document's content the Mark of the Web (an NTFS Zone.Identifier stream
    saying "Internet"), as browsers and mail clients do for downloads and attachments. A no-op elsewhere and
    on file systems without streams."""
    if sys.platform != "win32":
        return
    try:
        with open(path + ":Zone.Identifier", "w", encoding="ascii") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    except OSError:
        pass


def check_file(target: str, base_dir: str | None) -> Verdict:
    """A link to a local file (a PDF launch or remote go-to action, or a file: URL)."""
    f = str(target or "").strip()
    if not f:
        return Verdict("block", f, "The link has no target.")
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:[\\/]", f):  # MuPDF's form of file:///C:/...
        f = f[1:]
    if is_network_path(f):
        return Verdict("block", f, "Leaf doesn't open links to network locations from a document:\n\n" + shown(f))
    if not os.path.isabs(f) or (sys.platform == "win32" and not os.path.splitdrive(f)[0]):
        if not base_dir:
            return Verdict("block", f, "The link points to a file relative to a document that isn't saved yet.")
        f = os.path.join(base_dir, f.lstrip("\\/") if sys.platform == "win32" else f)
    f = os.path.normpath(f)
    if is_risky(f):
        return Verdict("block", f, "Leaf doesn't open programs, scripts or shortcuts from a document. The link "
                                   "points to:\n\n" + shown(f))
    if not os.path.exists(f):
        return Verdict("block", f, "The linked file wasn't found:\n\n" + shown(f))
    ext = os.path.splitext(f.rstrip(" ."))[1].lower()
    if os.path.isdir(f) or ext in DOCUMENTS:
        return Verdict("open", f, local=True)
    return Verdict("warn", f, "This link opens a file that isn't a document Leaf knows. Open it only if you trust "
                              "where this document came from.", local=True)


def check_uri(uri: str, base_dir: str | None = None) -> Verdict:
    """A URI link. Judged by the URL as Qt parses it, i.e. what openUrl would get."""
    from PySide6.QtCore import QUrl

    raw = str(uri or "").strip()
    url = QUrl(raw)
    if not raw or not url.isValid():
        return Verdict("block", raw, "The link isn't a valid address.")
    scheme = url.scheme().lower()
    if scheme == "file" or (sys.platform == "win32" and len(scheme) == 1):  # file:..., or a bare "C:\..." path
        return check_file(url.toLocalFile() if scheme == "file" else raw, base_dir)
    if not scheme:
        return check_file(raw, base_dir)
    if scheme in WEB_SCHEMES:
        if scheme != "mailto" and not url.host():
            return Verdict("block", raw, "The link isn't a valid web address.")
        return Verdict("open", url.toString())
    if scheme in BLOCKED_SCHEMES or scheme.startswith("ms-"):
        return Verdict("block", raw, f"Leaf doesn't open “{shown(scheme)}:” links from a document: they can run "
                                     "programs or reach network locations.")
    return Verdict("warn", url.toString(), f"This link opens another app (“{shown(scheme)}:” link). Open it only if "
                                           "you trust where this document came from.")
