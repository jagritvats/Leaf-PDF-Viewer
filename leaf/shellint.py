"""Shell integration: Leaf as a registered PDF app, "make Leaf the default", and sharing the current file.

Pure helpers (registry entries, settings URIs, command lines, merge order) are kept apart from the calls that
touch the system, so tests run them on any platform. Every system call is safe off-platform and returns False /
None instead of raising.

- Registration (Windows): `registry_entries(exe)` is what the installer (packaging/leaf.iss) writes and what
  `register()` writes for a portable (zip) copy: the Leaf.pdf ProgID, Capabilities + RegisteredApplications (lists
  Leaf in Settings › Apps › Default apps and in "How do you want to open this?"), the .pdf OpenWithProgids entry,
  and the classic Explorer verbs "Edit with Leaf" / "Combine with Leaf". Nothing takes over the default PDF app:
  Windows only lets the user choose it, so the in-app action opens the Default apps page for Leaf.
- Default app (Linux): `xdg-mime default <app-id>.desktop application/pdf` (not inside the Flatpak sandbox, which
  would only change its own copy of the setting).
- Share: the Windows share sheet through Explorer's own "Share" verb (Windows.ModernShare) on the file, so no
  WinRT projection is bundled; "Copy file" (a file on the clipboard: CF_HDROP on Windows), "Show in folder"
  (SHOpenFolderAndSelectItems / FileManager1.ShowItems) and "Email…" (Simple MAPI / xdg-email).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

APP_NAME = "Leaf"  # value name under RegisteredApplications (ms-settings ...?registeredAppUser=Leaf)
PROG_ID = "Leaf.pdf"
AUMID = "Leaf.PdfEditor"  # unpackaged taskbar identity (leaf/__main__.py); the ProgID carries it for the jump list
CAPS_KEY = r"Software\Leaf\Capabilities"
DESCRIPTION = "Edit, sign and redact PDF files on your computer."
SHELL_KEY = r"Software\Classes\SystemFileAssociations\.pdf\shell"
VERB_EDIT = "Leaf.edit"
VERB_COMBINE = "Leaf.combine"
PDF_MIME = "application/pdf"


# =========================================================================================== registration
def registry_entries(exe: str) -> list[tuple[str, str, str]]:
    """(key under HKCU/HKLM, value name ("" = default value), data) that register Leaf as a PDF app.

    The installer writes the same entries (a test parses packaging/leaf.iss and compares)."""
    open_cmd = f'"{exe}" "%1"'
    icon = f"{exe},0"
    cls = r"Software\Classes"
    return [
        (rf"{cls}\{PROG_ID}", "", "PDF document"),
        (rf"{cls}\{PROG_ID}", "FriendlyTypeName", "PDF document"),
        (rf"{cls}\{PROG_ID}", "AppUserModelID", AUMID),
        (rf"{cls}\{PROG_ID}\DefaultIcon", "", icon),
        (rf"{cls}\{PROG_ID}\shell\open", "FriendlyAppName", APP_NAME),
        (rf"{cls}\{PROG_ID}\shell\open\command", "", open_cmd),
        (rf"{cls}\.pdf\OpenWithProgids", PROG_ID, ""),
        (rf"{cls}\Applications\Leaf.exe", "FriendlyAppName", APP_NAME),
        (rf"{cls}\Applications\Leaf.exe\SupportedTypes", ".pdf", ""),
        (CAPS_KEY, "ApplicationName", APP_NAME),
        (CAPS_KEY, "ApplicationDescription", DESCRIPTION),
        (CAPS_KEY, "ApplicationIcon", icon),
        (rf"{CAPS_KEY}\FileAssociations", ".pdf", PROG_ID),
        (r"Software\RegisteredApplications", APP_NAME, CAPS_KEY),
        # classic context menu (Windows 11: "Show more options"); one process per selected file, which the running
        # Leaf gathers (leaf/ipc.py); Explorer hides Document-model verbs above 15 selected files
        (rf"{SHELL_KEY}\{VERB_EDIT}", "", "Edit with &Leaf"),
        (rf"{SHELL_KEY}\{VERB_EDIT}", "Icon", icon),
        (rf"{SHELL_KEY}\{VERB_EDIT}", "MultiSelectModel", "Document"),
        (rf"{SHELL_KEY}\{VERB_EDIT}\command", "", open_cmd),
        (rf"{SHELL_KEY}\{VERB_COMBINE}", "", "Com&bine with Leaf"),
        (rf"{SHELL_KEY}\{VERB_COMBINE}", "Icon", icon),
        (rf"{SHELL_KEY}\{VERB_COMBINE}", "MultiSelectModel", "Document"),
        (rf"{SHELL_KEY}\{VERB_COMBINE}\command", "", f'"{exe}" --merge "%1"'),
    ]


# keys that belong to Leaf alone (removed with everything under them) and values Leaf adds to shared keys
OWNED_KEYS = (
    rf"Software\Classes\{PROG_ID}",
    CAPS_KEY,
    rf"{SHELL_KEY}\{VERB_EDIT}",
    rf"{SHELL_KEY}\{VERB_COMBINE}",
    r"Software\Classes\Applications\Leaf.exe",
)
SHARED_VALUES = (
    (r"Software\Classes\.pdf\OpenWithProgids", PROG_ID),
    (r"Software\RegisteredApplications", APP_NAME),
)


def register(exe: str, reg=None, root=None) -> int:
    """Write `registry_entries(exe)` under `root` (default HKEY_CURRENT_USER). `reg` is the winreg module (a fake
    in tests). Returns the number of values written."""
    reg = reg or _winreg()
    root = reg.HKEY_CURRENT_USER if root is None else root
    n = 0
    for key, name, data in registry_entries(exe):
        with reg.CreateKeyEx(root, key, 0, reg.KEY_WRITE) as k:
            reg.SetValueEx(k, name, 0, reg.REG_SZ, data)
            n += 1
    return n


def unregister(reg=None, root=None) -> None:
    """Remove what `register()` wrote (Leaf's own keys with their subkeys, its values in shared keys)."""
    reg = reg or _winreg()
    root = reg.HKEY_CURRENT_USER if root is None else root
    for key, name in SHARED_VALUES:
        try:
            with reg.OpenKey(root, key, 0, reg.KEY_SET_VALUE) as k:
                reg.DeleteValue(k, name)
        except OSError:
            pass
    for key in OWNED_KEYS:
        _delete_tree(reg, root, key)
    # Software\Leaf also holds Leaf's settings (QSettings): only Capabilities goes


def _delete_tree(reg, root, key: str) -> None:
    try:
        with reg.OpenKey(root, key, 0, reg.KEY_READ) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(reg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        return
    for s in subs:
        _delete_tree(reg, root, key + "\\" + s)
    try:
        reg.DeleteKey(root, key)
    except OSError:
        pass


def _read(reg, root, key: str, name: str = "") -> str | None:
    try:
        with reg.OpenKey(root, key, 0, reg.KEY_READ) as k:
            v, _t = reg.QueryValueEx(k, name)
            return v if isinstance(v, str) else None
    except OSError:
        return None


def registered_scope(reg=None) -> str | None:
    """"user" / "machine" when Leaf is listed under RegisteredApplications (installer or `register()`), else None."""
    if reg is None:
        if not IS_WIN:
            return None
        reg = _winreg()
    for scope, root in (("user", reg.HKEY_CURRENT_USER), ("machine", reg.HKEY_LOCAL_MACHINE)):
        if _read(reg, root, r"Software\RegisteredApplications", APP_NAME):
            return scope
    return None


def registered_exe(reg=None, root=None) -> str | None:
    """The executable Leaf's ProgID opens (None when not registered there)."""
    if reg is None:
        if not IS_WIN:
            return None
        reg = _winreg()
    root = reg.HKEY_CURRENT_USER if root is None else root
    cmd = _read(reg, root, rf"Software\Classes\{PROG_ID}\shell\open\command")
    if not cmd:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
    return (m.group(1) or m.group(2)) if m else None


INNO_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6C1E5D2A-4F4B-4E59-9C7B-2B7A1E0F5D11}_is1"


def installed_by_installer(reg=None) -> bool:
    """The Inno Setup installer (packaging/leaf.iss AppId) installed Leaf for this user or the machine."""
    if reg is None:
        if not IS_WIN:
            return False
        reg = _winreg()
    return any(_read(reg, root, INNO_UNINSTALL_KEY, "DisplayName")
               for root in (reg.HKEY_CURRENT_USER, reg.HKEY_LOCAL_MACHINE))


def _winreg():
    import winreg

    return winreg


def notify_assoc_changed() -> None:
    """Tell Explorer that file associations changed (refreshes "Open with" and Default apps)."""
    if not IS_WIN:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
    except Exception:  # noqa: BLE001
        pass


# =========================================================================================== packaging state
def in_msix() -> bool:
    """Running from the Microsoft Store / MSIX package (Windows assigns the app identity then)."""
    if not IS_WIN:
        return False
    from .__main__ import _in_msix_package

    return _in_msix_package()


def package_aumid() -> str | None:
    """The packaged app's AppUserModelID (<PackageFamilyName>!Leaf), or None when not packaged."""
    if not IS_WIN:
        return None
    try:
        import ctypes

        n = ctypes.c_uint32(512)
        buf = ctypes.create_unicode_buffer(n.value)
        if ctypes.windll.kernel32.GetCurrentApplicationUserModelId(ctypes.byref(n), buf) == 0:
            return buf.value or None
    except Exception:  # noqa: BLE001
        pass
    return None


def frozen_exe() -> str | None:
    """The Leaf.exe of a packaged (PyInstaller) build; None when running from source (python -m leaf)."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0


# =========================================================================================== default app
def default_apps_uri(build: int, scope: str | None = None, aumid: str | None = None) -> str:
    """The Settings page where the user makes Leaf the default PDF app.

    Windows 11 opens Leaf's own page (registeredAUMID for the Store package, registeredAppUser/Machine for the
    installed app); Windows 10 has no per-app page, so it gets Default apps (choose "Choose defaults by file type")."""
    from urllib.parse import quote

    if build < 22000:
        return "ms-settings:defaultapps"
    if aumid:
        return "ms-settings:defaultapps?registeredAUMID=" + quote(aumid, safe="")
    if scope == "machine":
        return f"ms-settings:defaultapps?registeredAppMachine={APP_NAME}"
    if scope == "user":
        return f"ms-settings:defaultapps?registeredAppUser={APP_NAME}"
    return "ms-settings:defaultapps"


def desktop_id() -> str:
    from .__main__ import APP_ID

    return APP_ID + ".desktop"


def in_flatpak(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


def data_dirs(env=None) -> list[str]:
    """XDG data directories, most important first ($XDG_DATA_HOME, then $XDG_DATA_DIRS)."""
    env = os.environ if env is None else env
    home = env.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [home] + [d for d in dirs.split(":") if d]


def find_desktop_file(did: str, env=None) -> str | None:
    for d in data_dirs(env):
        p = os.path.join(d, "applications", did)
        if os.path.isfile(p):
            return p
    return None


def xdg_default_command(did: str, mime: str = PDF_MIME) -> list[str]:
    return ["xdg-mime", "default", did, mime]


def xdg_query_command(mime: str = PDF_MIME) -> list[str]:
    return ["xdg-mime", "query", "default", mime]


def default_app_plan() -> dict:
    """What "Make Leaf the default PDF app" does here: {"kind": ..., "text": what to tell the user first}.

    kinds: "msix" / "registered" (open Settings), "unregistered" (offer to register this copy first), "source"
    (running from source on Windows: nothing to register), "xdg" (run xdg-mime), "flatpak" / "no-desktop-file" /
    "no-xdg-mime" (explain), "unsupported"."""
    if IS_WIN:
        if in_msix():
            return {"kind": "msix", "text": "Windows doesn't let apps make themselves the default. Settings opens on "
                                            "Leaf's page: choose Leaf for .pdf there."}
        if registered_scope():
            return {"kind": "registered", "text": "Windows doesn't let apps make themselves the default. Settings "
                                                  "opens on Leaf's page: choose Leaf for .pdf there."}
        if frozen_exe():
            return {"kind": "unregistered", "text": "This copy of Leaf isn't registered with Windows yet (the "
                    "installer does that). Register it for your account so it shows up in Default apps and in "
                    "Open with?"}
        return {"kind": "source", "text": "Leaf runs from source here: install it (or use the zip build) to "
                                          "register it with Windows."}
    if IS_LINUX:
        did = desktop_id()
        if in_flatpak():
            return {"kind": "flatpak", "text": "From the Flatpak, Leaf can't change the system's default apps. Choose "
                    "Leaf for PDF files in your system settings (Default applications) or in your file manager's "
                    "Open With dialog."}
        if find_desktop_file(did) is None:
            return {"kind": "no-desktop-file", "text": f"Leaf's desktop entry ({did}) isn't installed, so it can't "
                    "be the default PDF app. Install Leaf from the AUR package or the tarball's desktop file first "
                    "(an AppImage needs an integration tool such as Gear Lever or AppImageLauncher)."}
        if not shutil.which("xdg-mime"):
            return {"kind": "no-xdg-mime", "text": "xdg-mime (xdg-utils) isn't installed. Choose Leaf for PDF files "
                                                   "in your system settings or file manager instead."}
        return {"kind": "xdg", "text": f"Make Leaf the default app for PDF files?\n\nThis runs: "
                                       f"{' '.join(xdg_default_command(did))}"}
    return {"kind": "unsupported", "text": "Choose Leaf for PDF files in your system's settings."}


def open_uri(uri: str) -> bool:
    if IS_WIN:
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        return QDesktopServices.openUrl(QUrl(uri))
    except Exception:  # noqa: BLE001
        return False


def open_default_apps_settings() -> bool:
    """Windows: open Settings › Default apps on Leaf's page."""
    aumid = package_aumid() if in_msix() else None
    return open_uri(default_apps_uri(windows_build(), registered_scope(), aumid))


def set_linux_default(run=subprocess.run, did: str | None = None) -> tuple[bool, str]:
    """xdg-mime default <app-id>.desktop application/pdf, then read it back. (ok, message for the user)."""
    did = did or desktop_id()
    try:
        r = run(xdg_default_command(did), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as ex:
        return False, f"Couldn't run xdg-mime: {ex}"
    if r.returncode != 0:
        return False, f"xdg-mime failed: {(r.stderr or r.stdout or '').strip() or r.returncode}"
    try:
        q = run(xdg_query_command(), capture_output=True, text=True, timeout=15)
        now = (q.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        now = ""
    if now and now != did:
        return False, f"The default PDF app is still {now} (your desktop may override xdg-mime)."
    return True, "Leaf is now the default app for PDF files."


# =========================================================================================== merge
def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", os.path.basename(name))]


def merge_order(batches: list[list[str]]) -> list[str]:
    """The files of one "Combine" in order. Explorer starts one process per selected file in no useful order, so
    single-file requests are sorted by name (naturally: 2 before 10); one request with several files (the command
    line, Send to, a Linux file manager's %F) keeps its order. Duplicates are dropped."""
    flat = [p for b in batches for p in b]
    if len(batches) > 1 and all(len(b) <= 1 for b in batches):
        flat = sorted(flat, key=_natural)
    seen, out = set(), []
    for p in flat:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def combined_name(folder: str, exists=os.path.exists) -> str:
    """A path for the combined document that doesn't exist yet: Combined.pdf, Combined (2).pdf, ..."""
    for i in range(1, 1000):
        p = os.path.join(folder, "Combined.pdf" if i == 1 else f"Combined ({i}).pdf")
        if not exists(p):
            return p
    return os.path.join(folder, "Combined.pdf")


# =========================================================================================== share
SHARE_DIR = os.path.join(tempfile.gettempdir(), "leaf-share")
SHARE_KEEP = 24 * 3600  # seconds a shared temp copy stays (the target may read it long after: mail, clipboard)


def temp_share_path(name: str, now: float | None = None) -> str:
    """A fresh temp path named `name` (the receiver sees the document's name); old copies are cleaned up."""
    now = time.time() if now is None else now
    try:
        for d in os.listdir(SHARE_DIR):
            full = os.path.join(SHARE_DIR, d)
            if now - os.path.getmtime(full) > SHARE_KEEP:
                shutil.rmtree(full, ignore_errors=True)
    except OSError:
        pass
    folder = tempfile.mkdtemp(prefix="s", dir=_ensure(SHARE_DIR))
    base = re.sub(r'[\\/:*?"<>|]+', "_", name or "Document.pdf")
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return os.path.join(folder, base)


def _ensure(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


def file_mime(paths: list[str]):
    """QMimeData that pastes as files: URLs (Qt makes CF_HDROP of local file URLs on Windows, text/uri-list on
    Linux) plus GNOME's copied-files list, which Nautilus needs to paste a file."""
    from PySide6.QtCore import QByteArray, QMimeData, QUrl

    md = QMimeData()
    urls = [QUrl.fromLocalFile(os.path.abspath(p)) for p in paths]
    md.setUrls(urls)
    if not IS_WIN:
        md.setData("x-special/gnome-copied-files",
                   QByteArray(("copy\n" + "\n".join(u.toString() for u in urls)).encode("utf-8")))
    if IS_WIN:
        # Explorer: paste copies (doesn't move) the file
        md.setData('application/x-qt-windows-mime;value="Preferred DropEffect"', QByteArray(b"\x01\x00\x00\x00"))
    return md


def copy_file_to_clipboard(paths: list[str]) -> None:
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setMimeData(file_mime(paths))


def explorer_select_command(path: str) -> str:
    """Fallback for Show in folder: explorer.exe parses `/select,"<path>"` itself (a string, not a list)."""
    return f'explorer.exe /select,"{os.path.normpath(path)}"'


def reveal(path: str) -> bool:
    """Show `path` selected in the file manager. Falls back to opening its folder."""
    path = os.path.abspath(path)
    if IS_WIN:
        if _win_reveal(path):
            return True
        try:
            subprocess.Popen(explorer_select_command(path))  # noqa: S603 - fixed program, quoted path
            return True
        except OSError:
            return False
    if IS_MAC:
        try:
            subprocess.Popen(["open", "-R", path])  # noqa: S603,S607
            return True
        except OSError:
            return False
    if _dbus_show_items(path):
        return True
    return open_uri_local(os.path.dirname(path))


def open_uri_local(folder: str) -> bool:
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    except Exception:  # noqa: BLE001
        return False


def _win_pidl(path: str):
    import ctypes

    pidl = ctypes.c_void_p()
    fn = ctypes.windll.shell32.SHParseDisplayName
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.c_void_p]
    fn.restype = ctypes.c_long
    if fn(path, None, ctypes.byref(pidl), 0, None) != 0 or not pidl.value:
        return None
    return pidl


def _win_reveal(path: str) -> bool:
    try:
        import ctypes

        pidl = _win_pidl(path)
        if pidl is None:
            return False
        try:
            fn = ctypes.windll.shell32.SHOpenFolderAndSelectItems
            fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
            fn.restype = ctypes.c_long
            return fn(pidl, 0, None, 0) == 0
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
    except Exception:  # noqa: BLE001
        return False


def _dbus_show_items(path: str) -> bool:
    """org.freedesktop.FileManager1.ShowItems (Nautilus, Dolphin, Nemo, Thunar, ...)."""
    if not IS_LINUX:
        return False
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        iface = QDBusInterface("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                               "org.freedesktop.FileManager1", bus)
        if not iface.isValid():
            return False
        reply = iface.call("ShowItems", [QUrl.fromLocalFile(path).toString()], "")
        return reply.type() != QDBusMessage.MessageType.ErrorMessage
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------------------------- email
def mail_client(read=None) -> str | None:
    """Windows: the name of the default Simple MAPI mail client (HKCU, then HKLM Software\\Clients\\Mail), or
    None. `read(root_name, key)` returns a default value (tests)."""
    if read is None:
        if not IS_WIN:
            return None
        reg = _winreg()

        def read(root, key):
            return _read(reg, getattr(reg, root), key)

    for root in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
        v = read(root, r"Software\Clients\Mail")
        if v and v.strip():
            return v.strip()
    return None


def xdg_email_command(path: str, subject: str) -> list[str]:
    return ["xdg-email", "--subject", subject, "--attach", os.path.abspath(path)]


def email_available() -> bool:
    if IS_WIN:
        return mail_client() is not None
    if IS_LINUX:
        return not in_flatpak() and shutil.which("xdg-email") is not None
    return False


def send_email(path: str, subject: str, done=None) -> bool:
    """Open the mail client's compose window with `path` attached. Windows: MAPISendMailW on a worker thread (it
    blocks until the message is sent or closed); `done(code)` is called there with the MAPI result (0 = sent,
    1 = cancelled). Linux: xdg-email."""
    if IS_WIN:
        import threading

        def work():
            code = _mapi_send(path, subject)
            if done is not None:
                done(code)

        threading.Thread(target=work, daemon=True, name="leaf-mapi").start()
        return True
    if IS_LINUX:
        try:
            subprocess.Popen(xdg_email_command(path, subject), start_new_session=True)  # noqa: S603
            return True
        except OSError:
            return False
    return False


MAPI_LOGON_UI, MAPI_DIALOG = 0x1, 0x8


def _mapi_send(path: str, subject: str) -> int:
    try:
        import ctypes
        from ctypes import wintypes as W

        class FileDesc(ctypes.Structure):
            _fields_ = [("ulReserved", W.ULONG), ("flFlags", W.ULONG), ("nPosition", W.ULONG),
                        ("lpszPathName", W.LPWSTR), ("lpszFileName", W.LPWSTR), ("lpFileType", ctypes.c_void_p)]

        class Message(ctypes.Structure):
            _fields_ = [("ulReserved", W.ULONG), ("lpszSubject", W.LPWSTR), ("lpszNoteText", W.LPWSTR),
                        ("lpszMessageType", W.LPWSTR), ("lpszDateReceived", W.LPWSTR),
                        ("lpszConversationID", W.LPWSTR), ("flFlags", W.ULONG), ("lpOriginator", ctypes.c_void_p),
                        ("nRecipCount", W.ULONG), ("lpRecips", ctypes.c_void_p), ("nFileCount", W.ULONG),
                        ("lpFiles", ctypes.POINTER(FileDesc))]

        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        f = FileDesc(0, 0, 0xFFFFFFFF, os.path.abspath(path), os.path.basename(path), None)
        msg = Message(0, subject, "", None, None, None, 0, None, 0, None, 1, ctypes.pointer(f))
        fn = ctypes.WinDLL("mapi32.dll").MAPISendMailW
        fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Message), W.ULONG, W.ULONG]
        fn.restype = W.ULONG
        return int(fn(None, 0, ctypes.byref(msg), MAPI_LOGON_UI | MAPI_DIALOG, 0))
    except Exception:  # noqa: BLE001 - no mapi32 / no client
        return -1


# ------------------------------------------------------------------------------------------- share sheet
SHARE_VERB = "Windows.ModernShare"  # Explorer's "Share" (Windows 10 1709+ and 11)


def share_sheet_available() -> bool:
    return IS_WIN and windows_build() >= 16299


def show_share_sheet(path: str, hwnd: int | None = None) -> bool:
    """Windows: open the system share sheet for `path` by invoking Explorer's own Share verb on it (the same
    window as right-click › Share). Returns False when the verb isn't there (the caller falls back)."""
    if not share_sheet_available():
        return False
    try:
        return _invoke_verb(os.path.abspath(path), SHARE_VERB, hwnd)
    except Exception:  # noqa: BLE001
        return False


def _vcall(ptr, index: int, args: tuple, argtypes: tuple):
    import ctypes

    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])(ptr, *args)


def _guid(s: str):
    import ctypes
    import uuid

    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)


def _invoke_verb(path: str, verb: str, hwnd: int | None, invoke: bool = True) -> bool:
    """Find `verb` in the file's Explorer context menu and run it (`invoke=False`: only check it's there)."""
    import ctypes
    from ctypes import byref, c_uint, c_void_p

    shell32, user32, ole32 = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0x2)  # S_FALSE when Qt already did (OleInitialize: apartment threaded)
    pidl = _win_pidl(path)
    if pidl is None:
        return False
    folder, child, cm = c_void_p(), c_void_p(), c_void_p()
    hmenu = None
    try:
        bind = shell32.SHBindToParent
        bind.argtypes = [c_void_p, c_void_p, ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)]
        bind.restype = ctypes.c_long
        if bind(pidl, byref(_guid("000214E6-0000-0000-C000-000000000046")), byref(folder), byref(child)) != 0:
            return False
        items = (c_void_p * 1)(child)
        hr = _vcall(folder, 10, (hwnd, 1, items, byref(_guid("000214E4-0000-0000-C000-000000000046")), None, byref(cm)),
                    (c_void_p, c_uint, c_void_p, c_void_p, c_void_p, ctypes.POINTER(c_void_p)))  # GetUIObjectOf
        if hr != 0 or not cm.value:
            return False
        user32.CreatePopupMenu.restype = c_void_p
        hmenu = user32.CreatePopupMenu()
        first = 1
        hr = _vcall(cm, 3, (hmenu, 0, first, 0x7FFF, 0), (c_void_p, c_uint, c_uint, c_uint, c_uint))  # QueryContextMenu
        if hr < 0:
            return False
        cmd = _find_verb(cm, hmenu, verb, first)
        if cmd is None or not invoke:
            return cmd is not None

        class Invoke(ctypes.Structure):  # CMINVOKECOMMANDINFO
            _fields_ = [("cbSize", ctypes.c_uint32), ("fMask", ctypes.c_uint32), ("hwnd", c_void_p),
                        ("lpVerb", c_void_p), ("lpParameters", c_void_p), ("lpDirectory", c_void_p),
                        ("nShow", ctypes.c_int), ("dwHotKey", ctypes.c_uint32), ("hIcon", c_void_p)]

        info = Invoke(ctypes.sizeof(Invoke), 0, hwnd, c_void_p(cmd - first), None, None, 1, 0, None)  # MAKEINTRESOURCE
        return _vcall(cm, 4, (byref(info),), (c_void_p,)) == 0  # InvokeCommand
    finally:
        if hmenu:
            user32.DestroyMenu.argtypes = [c_void_p]
            user32.DestroyMenu(hmenu)
        for p in (cm, folder):
            if p.value:
                _vcall(p, 2, (), ())  # Release
        ole32.CoTaskMemFree(pidl)


def _find_verb(cm, hmenu, verb: str, first: int) -> int | None:
    """The command id of the item whose canonical verb is `verb` (top level of the context menu)."""
    import ctypes
    from ctypes import c_uint, c_void_p

    user32 = ctypes.windll.user32
    user32.GetMenuItemCount.argtypes = [c_void_p]
    user32.GetMenuItemID.argtypes = [c_void_p, ctypes.c_int]
    user32.GetMenuItemID.restype = ctypes.c_int
    buf = ctypes.create_unicode_buffer(260)
    for i in range(user32.GetMenuItemCount(hmenu)):
        cid = user32.GetMenuItemID(hmenu, i)
        if cid < first:  # separators (0) and submenus (-1)
            continue
        buf.value = ""
        hr = _vcall(cm, 5, (cid - first, 4, None, buf, 260), (ctypes.c_size_t, c_uint, c_void_p, ctypes.c_wchar_p, c_uint))
        if hr == 0 and buf.value.lower() == verb.lower():  # GCS_VERBW
            return cid
    return None


# ------------------------------------------------------------------------------------------- jump list
def jump_list_tasks(exe: str) -> list[dict]:
    """The Tasks of Leaf's taskbar / Start jump list."""
    return [{"title": "New window", "exe": exe, "args": "--new-window", "icon": exe, "tip": "Open another Leaf window"}]


def set_jump_list(tasks: list[dict], app_id: str | None = AUMID) -> bool:
    """Windows: replace the Tasks of the jump list (ICustomDestinationList). `app_id` None = the process's own
    identity (the MSIX package's). The "Recent" category stays Windows' own (files opened through Leaf.pdf)."""
    if not IS_WIN:
        return False
    try:
        return _set_jump_list(tasks, app_id)
    except Exception:  # noqa: BLE001 - cosmetic only
        return False


def delete_jump_list(app_id: str) -> bool:
    if not IS_WIN:
        return False
    try:
        dl = _co_create("77f10cf0-3db5-4966-b520-b7c54fd35ed6", "6332debf-87b5-4670-90c0-5e57b408a49e")
        if dl is None:
            return False
        try:
            import ctypes

            return _vcall(dl, 10, (app_id,), (ctypes.c_wchar_p,)) == 0  # DeleteList
        finally:
            _vcall(dl, 2, (), ())
    except Exception:  # noqa: BLE001
        return False


def _co_create(clsid: str, iid: str):
    import ctypes

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0x2)
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(clsid)), None, 1, ctypes.byref(_guid(iid)), ctypes.byref(ptr))
    return ptr if hr == 0 and ptr.value else None


def _set_jump_list(tasks: list[dict], app_id: str | None) -> bool:
    import ctypes
    from ctypes import POINTER, byref, c_uint, c_void_p, c_wchar_p

    dl = _co_create("77f10cf0-3db5-4966-b520-b7c54fd35ed6", "6332debf-87b5-4670-90c0-5e57b408a49e")  # DestinationList
    if dl is None:
        return False
    coll = _co_create("2d3468c1-36a7-43b6-ac24-d3f02fd9607a", "5632b1a4-e38a-400a-928a-d4cd63230295")  # IObjectCollection
    links = []
    try:
        if coll is None:
            return False
        if app_id and _vcall(dl, 3, (app_id,), (c_wchar_p,)) != 0:  # SetAppID
            return False
        slots, removed = c_uint(0), c_void_p()
        if _vcall(dl, 4, (byref(slots), byref(_guid("92CA9DCD-5622-4bba-A805-5E9F541BD8C9")), byref(removed)),
                  (POINTER(c_uint), c_void_p, POINTER(c_void_p))) != 0:  # BeginList
            return False
        if removed.value:
            _vcall(removed, 2, (), ())
        # a custom list hides Windows' automatic "Recent" unless it is asked for (fails harmlessly when Leaf isn't
        # registered for a file type: a portable run has no Recent category anyway)
        _vcall(dl, 6, (2,), (ctypes.c_int,))  # AppendKnownCategory(KDC_RECENT)
        for t in tasks:
            link = _co_create("00021401-0000-0000-C000-000000000046", "000214F9-0000-0000-C000-000000000046")
            if link is None:
                return False
            links.append(link)
            _vcall(link, 20, (t["exe"],), (c_wchar_p,))  # SetPath
            _vcall(link, 11, (t.get("args", ""),), (c_wchar_p,))  # SetArguments
            _vcall(link, 7, (t.get("tip", t["title"]),), (c_wchar_p,))  # SetDescription
            _vcall(link, 17, (t.get("icon", t["exe"]), 0), (c_wchar_p, ctypes.c_int))  # SetIconLocation
            if not _set_title(link, t["title"]):
                return False
            if _vcall(coll, 5, (link,), (c_void_p,)) != 0:  # AddObject
                return False
        if _vcall(dl, 7, (coll,), (c_void_p,)) != 0:  # AddUserTasks (an IObjectCollection is an IObjectArray)
            _vcall(dl, 11, (), ())  # AbortList
            return False
        return _vcall(dl, 8, (), ()) == 0  # CommitList
    finally:
        for p in links + [coll, dl]:
            if p is not None and p.value:
                _vcall(p, 2, (), ())


def _set_title(link, title: str) -> bool:
    """PKEY_Title on the shell link: the text the jump list shows."""
    import ctypes
    from ctypes import POINTER, byref, c_void_p

    class PropKey(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", ctypes.c_uint32)]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("val", c_void_p), ("pad", c_void_p)]

    store = c_void_p()
    if _vcall(link, 0, (byref(_guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")), byref(store)),
              (c_void_p, POINTER(c_void_p))) != 0:  # QueryInterface(IPropertyStore)
        return False
    try:
        key = PropKey(_guid("F29F85E0-4FF9-1068-AB91-08002B27B3D9"), 2)
        text = ctypes.create_unicode_buffer(title)
        pv = PropVariant(31, 0, 0, 0, ctypes.cast(text, c_void_p), None)  # VT_LPWSTR (borrowed; the store copies)
        if _vcall(store, 6, (byref(key), byref(pv)), (c_void_p, c_void_p)) != 0:  # SetValue
            return False
        return _vcall(store, 7, (), ()) == 0  # Commit
    finally:
        _vcall(store, 2, (), ())
