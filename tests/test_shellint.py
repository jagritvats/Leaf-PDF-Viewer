"""Shell integration (leaf/shellint.py) as pure functions: the registry entries (checked against the installer
script), registration against a fake winreg, Settings URIs, xdg-mime/xdg-email command lines, the merge order, and
the packaging files for Explorer, the Store package and Linux file managers. Nothing here touches the real
registry, the clipboard or the desktop."""
from __future__ import annotations

import configparser
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from leaf import shellint as S

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXE = r"C:\Users\me\AppData\Local\Programs\Leaf\Leaf.exe"


# ============================================================== registry entries vs the installer
def _iss_registry() -> list[dict]:
    """The [Registry] lines of packaging/leaf.iss as dicts (Root, Subkey, ValueType, ValueName, ValueData, Flags)."""
    with open(os.path.join(ROOT, "packaging", "leaf.iss"), encoding="utf-8") as f:
        text = f.read()
    sec = re.search(r"^\[Registry\]\n(.*?)(?=^\[)", text, re.S | re.M).group(1)
    out = []
    for line in sec.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        d = {}
        for m in re.finditer(r'(\w+):\s*("(?:[^"]|"")*"|[^;]+)', line):
            v = m.group(2).strip()
            if v.startswith('"'):
                v = v[1:-1].replace('""', '"')
            d[m.group(1)] = v
        out.append(d)
    return out


def test_installer_writes_exactly_the_shared_registry_entries():
    rows = _iss_registry()
    assert all(r["Root"] == "HKA" for r in rows)  # per-user install: HKCU (HKLM when installed for all users)
    written = {(r["Subkey"], r["ValueName"], r["ValueData"]) for r in rows if "ValueType" in r}
    assert all(r.get("ValueType", "string") == "string" for r in rows)
    expected = {(k, n, v) for k, n, v in S.registry_entries(r"{app}\Leaf.exe")}
    assert written == expected


def test_installer_uninstall_removes_everything_in_reverse_order():
    rows = _iss_registry()
    flags = lambda r: set((r.get("Flags") or "").split())  # noqa: E731
    deleted_keys = {r["Subkey"] for r in rows if "uninsdeletekey" in flags(r)}
    for key in S.OWNED_KEYS:
        if key.endswith(r"Applications\Leaf.exe"):  # shared with Windows' "Open with" bookkeeping: only if empty
            assert any(r["Subkey"] == key and "uninsdeletekeyifempty" in flags(r) for r in rows)
        else:
            assert key in deleted_keys, key
    for key, name in S.SHARED_VALUES:
        assert any(r["Subkey"] == key and r["ValueName"] == name and "uninsdeletevalue" in flags(r) for r in rows)
    # Software\Leaf also holds the user's settings (QSettings): never deleted with its contents
    assert not any(r["Subkey"] == r"Software\Leaf" and "uninsdeletekey" in flags(r) for r in rows)
    # uninstall runs the lines bottom-up: a key removed "if empty" comes before everything inside it
    for i, r in enumerate(rows):
        if "uninsdeletekeyifempty" in flags(r):
            inside = [j for j, o in enumerate(rows) if o["Subkey"].startswith(r["Subkey"] + "\\") or
                      (o["Subkey"] == r["Subkey"] and "ValueType" in o)]
            assert inside and min(inside) > i, r["Subkey"]
    # every value in a key Leaf owns goes with the key; the rest are deleted one by one
    for r in rows:
        if "ValueType" in r and not any(r["Subkey"] == k or r["Subkey"].startswith(k + "\\") for k in deleted_keys):
            assert "uninsdeletevalue" in flags(r), r


def test_installer_send_to_and_tasks():
    with open(os.path.join(ROOT, "packaging", "leaf.iss"), encoding="utf-8") as f:
        text = f.read()
    assert re.search(r'^Name: "\{usersendto\}\\Combine with Leaf"; Filename: "\{app\}\\Leaf.exe"; Parameters: "--merge"',
                     text, re.M)
    assert "ChangesAssociations=yes" in text


def test_registry_entries_shape():
    rows = S.registry_entries(EXE)
    d = {(k, n): v for k, n, v in rows}
    assert d[(r"Software\RegisteredApplications", "Leaf")] == S.CAPS_KEY
    assert d[(S.CAPS_KEY + r"\FileAssociations", ".pdf")] == "Leaf.pdf"
    assert d[(r"Software\Classes\Leaf.pdf\shell\open\command", "")] == f'"{EXE}" "%1"'
    assert d[(S.SHELL_KEY + r"\Leaf.combine\command", "")] == f'"{EXE}" --merge "%1"'
    assert d[(S.SHELL_KEY + r"\Leaf.edit", "MultiSelectModel")] == "Document"
    assert d[(r"Software\Classes\Leaf.pdf", "AppUserModelID")] == "Leaf.PdfEditor"
    from leaf import osint

    assert osint.APP_ID == S.AUMID


# ============================================================== register / unregister (fake winreg)
class FakeReg:
    """The part of winreg that shellint uses, over a dict {root: {key_lower: (key, {name: value})}}."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ = KEY_WRITE = KEY_SET_VALUE = 0
    REG_SZ = 1

    def __init__(self):
        self.data: dict = {"HKCU": {}, "HKLM": {}}

    class _K:
        def __init__(self, reg, root, key):
            self.reg, self.root, self.key = reg, root, key

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _keys(self, root):
        return self.data[root]

    def CreateKeyEx(self, root, key, _r=0, _a=0):  # noqa: N802
        parts = key.split("\\")
        for i in range(1, len(parts) + 1):
            k = "\\".join(parts[:i])
            self._keys(root).setdefault(k.lower(), (k, {}))
        return self._K(self, root, key)

    def OpenKey(self, root, key, _r=0, _a=0):  # noqa: N802
        if key.lower() not in self._keys(root):
            raise FileNotFoundError(key)
        return self._K(self, root, key)

    def SetValueEx(self, k, name, _r, _t, data):  # noqa: N802
        self._keys(k.root)[k.key.lower()][1][name] = data

    def QueryValueEx(self, k, name):  # noqa: N802
        vals = self._keys(k.root)[k.key.lower()][1]
        if name not in vals:
            raise FileNotFoundError(name)
        return vals[name], 1

    def DeleteValue(self, k, name):  # noqa: N802
        vals = self._keys(k.root)[k.key.lower()][1]
        if name not in vals:
            raise FileNotFoundError(name)
        del vals[name]

    def EnumKey(self, k, i):  # noqa: N802
        pre = k.key.lower() + "\\"
        kids = sorted({v[0][len(pre):].split("\\")[0] for kk, v in self._keys(k.root).items()
                       if kk.startswith(pre)})
        if i >= len(kids):
            raise OSError("no more")
        return kids[i]

    def DeleteKey(self, root, key):  # noqa: N802
        pre = key.lower() + "\\"
        if any(kk.startswith(pre) for kk in self._keys(root)):
            raise PermissionError("has subkeys")
        self._keys(root).pop(key.lower(), None)

    def value(self, root, key, name=""):
        return self._keys(root).get(key.lower(), (None, {}))[1].get(name)


def test_register_and_unregister_with_fake_registry():
    reg = FakeReg()
    # what the user (or Windows, other apps) already had: kept
    with reg.CreateKeyEx("HKCU", r"Software\Classes\.pdf\OpenWithProgids") as k:
        reg.SetValueEx(k, "Acrobat.Document.DC", 0, 1, "")
    with reg.CreateKeyEx("HKCU", r"Software\RegisteredApplications") as k:
        reg.SetValueEx(k, "Firefox", 0, 1, r"Software\Clients\StartMenuInternet\Firefox\Capabilities")
    with reg.CreateKeyEx("HKCU", r"Software\Leaf\Leaf") as k:  # Leaf's own settings (QSettings)
        reg.SetValueEx(k, "geometry", 0, 1, "x")

    assert S.registered_scope(reg) is None and S.registered_exe(reg) is None
    n = S.register(EXE, reg)
    assert n == len(S.registry_entries(EXE))
    assert S.registered_scope(reg) == "user"
    assert S.registered_exe(reg) == EXE
    assert reg.value("HKCU", r"Software\Classes\.pdf\OpenWithProgids", "Leaf.pdf") == ""
    assert reg.value("HKCU", S.CAPS_KEY, "ApplicationName") == "Leaf"

    S.unregister(reg)
    assert S.registered_scope(reg) is None and S.registered_exe(reg) is None
    left = {v[0] for v in reg.data["HKCU"].values()}
    for key in S.OWNED_KEYS:
        assert key not in left
    assert reg.value("HKCU", r"Software\Classes\.pdf\OpenWithProgids", "Acrobat.Document.DC") == ""
    assert reg.value("HKCU", r"Software\RegisteredApplications", "Firefox")
    assert reg.value("HKCU", r"Software\Leaf\Leaf", "geometry") == "x"
    assert not any(k.startswith(S.SHELL_KEY.lower() + "\\leaf.") for k in reg.data["HKCU"])
    S.unregister(reg)  # twice: nothing to do, no error


def test_machine_scope_and_installer_detection():
    reg = FakeReg()
    with reg.CreateKeyEx("HKLM", r"Software\RegisteredApplications") as k:
        reg.SetValueEx(k, "Leaf", 0, 1, S.CAPS_KEY)
    assert S.registered_scope(reg) == "machine"
    assert not S.installed_by_installer(reg)
    with reg.CreateKeyEx("HKCU", S.INNO_UNINSTALL_KEY) as k:
        reg.SetValueEx(k, "DisplayName", 0, 1, "Leaf version 0.2.1")
    assert S.installed_by_installer(reg)
    # the installer's AppId is the one in leaf.iss
    with open(os.path.join(ROOT, "packaging", "leaf.iss"), encoding="utf-8") as f:
        app_id = re.search(r"^AppId=\{(\{[0-9A-F-]+\})", f.read(), re.M).group(1)
    assert app_id + "_is1" in S.INNO_UNINSTALL_KEY


# ============================================================== default app
def test_default_apps_uri():
    assert S.default_apps_uri(19045, "user") == "ms-settings:defaultapps"  # Windows 10: no per-app page
    assert S.default_apps_uri(22631, "user") == "ms-settings:defaultapps?registeredAppUser=Leaf"
    assert S.default_apps_uri(26100, "machine") == "ms-settings:defaultapps?registeredAppMachine=Leaf"
    assert S.default_apps_uri(26100, None) == "ms-settings:defaultapps"
    uri = S.default_apps_uri(26100, None, "12345Owner.Leaf_abcdefgh!Leaf")
    assert uri == "ms-settings:defaultapps?registeredAUMID=12345Owner.Leaf_abcdefgh%21Leaf"


def test_linux_default_commands_and_desktop_lookup(tmp_path, monkeypatch):
    did = S.desktop_id()
    from leaf.__main__ import APP_ID

    assert did == APP_ID + ".desktop"
    assert S.xdg_default_command(did) == ["xdg-mime", "default", did, "application/pdf"]
    assert S.xdg_query_command() == ["xdg-mime", "query", "default", "application/pdf"]
    monkeypatch.chdir(tmp_path)  # relative dirs: XDG_DATA_DIRS is ":"-separated (a drive letter would split it)
    env = {"XDG_DATA_HOME": "home", "XDG_DATA_DIRS": "usr:/nonexistent"}
    assert S.data_dirs(env) == ["home", "usr", "/nonexistent"]
    assert S.find_desktop_file(did, env) is None
    os.makedirs(os.path.join("usr", "applications"))
    with open(os.path.join("usr", "applications", did), "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\n")
    assert S.find_desktop_file(did, env) == os.path.join("usr", "applications", did)
    assert S.in_flatpak({"FLATPAK_ID": APP_ID})


def test_set_linux_default_reads_back():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = "leaf.desktop\n" if cmd[1] == "query" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    ok, msg = S.set_linux_default(run, "leaf.desktop")
    assert ok and "default" in msg
    assert calls == [S.xdg_default_command("leaf.desktop"), S.xdg_query_command()]

    def overridden(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "org.gnome.Evince.desktop\n" if cmd[1] == "query" else "", "")

    ok, msg = S.set_linux_default(overridden, "leaf.desktop")
    assert not ok and "Evince" in msg

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 4, "", "xdg-mime: no writable mimeapps.list")

    ok, msg = S.set_linux_default(failing, "leaf.desktop")
    assert not ok and "mimeapps" in msg

    def missing(cmd, **kw):
        raise FileNotFoundError("xdg-mime")

    assert S.set_linux_default(missing, "leaf.desktop")[0] is False


# ============================================================== merge
def test_merge_order():
    a, b, c = os.path.abspath("x/scan 10.pdf"), os.path.abspath("x/scan 2.pdf"), os.path.abspath("x/A.pdf")
    # Explorer: one file per launch, arriving in any order -> sorted by name (2 before 10)
    assert S.merge_order([[a], [b], [c]]) == [c, b, a]
    # one launch with several files (command line, Send to, %F): their order
    assert S.merge_order([[a, b, c]]) == [a, b, c]
    assert S.merge_order([[a, b], [c]]) == [a, b, c]
    assert S.merge_order([[a], [a.upper() if os.name == "nt" else a], [b]]) == [b, a]
    assert S.merge_order([]) == []


def test_combined_name():
    taken = {os.path.join("d", "Combined.pdf"), os.path.join("d", "Combined (2).pdf")}
    assert S.combined_name("d", lambda p: p in taken) == os.path.join("d", "Combined (3).pdf")
    assert S.combined_name("e", lambda p: False) == os.path.join("e", "Combined.pdf")


# ============================================================== share helpers
def test_share_command_lines(tmp_path):
    p = str(tmp_path / "Report, final.pdf")
    assert S.explorer_select_command(p) == f'explorer.exe /select,"{os.path.normpath(p)}"'
    assert S.xdg_email_command(p, "Report") == ["xdg-email", "--subject", "Report", "--attach", os.path.abspath(p)]


def test_mail_client_lookup():
    reg = {("HKEY_LOCAL_MACHINE", r"Software\Clients\Mail"): "Microsoft Outlook"}
    assert S.mail_client(lambda root, key: reg.get((root, key))) == "Microsoft Outlook"
    reg[("HKEY_CURRENT_USER", r"Software\Clients\Mail")] = "Mozilla Thunderbird"
    assert S.mail_client(lambda root, key: reg.get((root, key))) == "Mozilla Thunderbird"
    assert S.mail_client(lambda root, key: "  ") is None


def test_temp_share_path_names_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SHARE_DIR", str(tmp_path / "share"))
    p = S.temp_share_path("Q3: report?.pdf")
    assert os.path.basename(p) == "Q3_ report_.pdf" and os.path.dirname(os.path.dirname(p)) == str(tmp_path / "share")
    assert os.path.basename(S.temp_share_path("Untitled")) == "Untitled.pdf"
    old = os.path.dirname(p)
    S.temp_share_path("x.pdf", now=os.path.getmtime(old) + S.SHARE_KEEP + 10)
    assert not os.path.exists(old)


def test_file_mime_has_file_urls(app, tmp_path):  # noqa: F811
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    md = S.file_mime([str(p)])
    assert [u.toLocalFile() for u in md.urls()] == [str(p).replace("\\", "/")]
    if S.IS_WIN:
        assert any("Preferred DropEffect" in f for f in md.formats())
    else:
        assert bytes(md.data("x-special/gnome-copied-files")).startswith(b"copy\nfile://")


@pytest.mark.skipif(not S.IS_WIN, reason="Windows shell")
def test_share_verb_lookup_never_invokes(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    # finds (or not, on a server SKU) Explorer's Share item without running it
    assert S._invoke_verb(str(p), S.SHARE_VERB, None, invoke=False) in (True, False)
    assert S._invoke_verb(str(p), "Leaf.no-such-verb", None, invoke=False) is False


# ============================================================== packaging files
def test_msix_declares_verbs():
    ns = {"m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
          "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
          "uap2": "http://schemas.microsoft.com/appx/manifest/uap/windows10/2",
          "uap3": "http://schemas.microsoft.com/appx/manifest/uap/windows10/3"}
    root = ET.parse(os.path.join(ROOT, "packaging", "msix", "AppxManifest.xml")).getroot()
    fta = root.find(".//uap3:FileTypeAssociation", ns)
    assert fta.get("Name") == "pdf" and fta.get("Parameters") == '"%1"'
    verbs = {v.get("Id"): (v.get("Parameters"), v.text) for v in fta.findall("uap2:SupportedVerbs/uap3:Verb", ns)}
    assert verbs == {"edit": ('"%1"', "Edit with Leaf"), "combine": ('--merge "%1"', "Combine with Leaf")}
    assert "uap2" in root.get("IgnorableNamespaces").split()


def test_linux_file_manager_actions():
    cp = configparser.ConfigParser(interpolation=None, strict=True)
    cp.optionxform = str
    cp.read(os.path.join(ROOT, "packaging", "linux", "leaf-combine.desktop"), encoding="utf-8")
    e = cp["Desktop Entry"]
    assert e["Type"] == "Service" and "application/pdf;" in e["MimeType"] and e["Actions"] == "combine;"
    assert cp["Desktop Action combine"]["Exec"] == "leaf --merge %F"
    from leaf.__main__ import APP_ID

    assert cp["Desktop Action combine"]["Icon"] == APP_ID
    with open(os.path.join(ROOT, "packaging", "linux", "nautilus-combine-with-leaf.sh"), encoding="utf-8") as f:
        sh = f.read()
    assert sh.startswith("#!/bin/sh") and 'exec leaf --merge "$@"' in sh
    with open(os.path.join(ROOT, "packaging", "build_linux.sh"), encoding="utf-8") as f:
        assert "leaf-combine.desktop" in f.read()
    with open(os.path.join(ROOT, "packaging", "aur", "PKGBUILD"), encoding="utf-8") as f:
        assert "/usr/share/kio/servicemenus/leaf-combine.desktop" in f.read()


from tests.test_editor import app  # noqa: E402,F401 - QApplication fixture (file_mime)


# ============================================================== jump list
def test_jump_list_tasks():
    (t,) = S.jump_list_tasks(EXE)
    assert t["title"] == "New window" and t["exe"] == EXE and t["args"] == "--new-window"


@pytest.mark.skipif(not S.IS_WIN, reason="Windows shell")
def test_jump_list_commit_on_a_throwaway_app_id():
    import sys
    import uuid

    aid = "Leaf.Test." + uuid.uuid4().hex[:10]  # not Leaf's own id: the user's jump list is left alone
    try:
        assert S.set_jump_list(S.jump_list_tasks(sys.executable), aid)
    finally:
        assert S.delete_jump_list(aid)
