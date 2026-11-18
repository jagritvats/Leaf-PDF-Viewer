"""Packaging metadata: AppStream metainfo, desktop entry, PyInstaller spec, CI workflow, Flatpak, AUR, MSIX.

Static checks only (no builds). The app id is read from the metainfo file name, so these keep passing after
packaging/set_app_id.py renames everything.
"""
from __future__ import annotations

import configparser
import os
import re
import xml.etree.ElementTree as ET

import pytest

from tests.test_editor import app  # noqa: F401 - QApplication fixture (test_msix_assets_render)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = os.path.join(ROOT, "packaging")
LINUX = os.path.join(PKG, "linux")


def _app_id() -> str:
    files = [f for f in os.listdir(LINUX) if f.endswith(".metainfo.xml")]
    assert len(files) == 1, files
    return files[0][: -len(".metainfo.xml")]


APP_ID = _app_id()


def _load(name: str, path: str):
    """Import a packaging script by path without leaving __pycache__ folders in packaging/."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    old, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _version() -> str:
    import tomllib

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_app_id_is_reverse_dns():
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2,}", APP_ID)
    assert "-" not in APP_ID


def test_metainfo_required_tags():
    root = ET.parse(os.path.join(LINUX, f"{APP_ID}.metainfo.xml")).getroot()
    assert root.tag == "component" and root.get("type") == "desktop-application"
    assert root.findtext("id") == APP_ID
    assert root.findtext("metadata_license") == "CC0-1.0"
    assert root.findtext("name") == "Leaf"
    assert root.findtext("summary")
    desc = root.find("description")
    assert desc is not None and desc.find("p") is not None and len(desc.findall("ul/li")) >= 5
    text = ET.tostring(desc, encoding="unicode").lower()
    for claim in ("redact", "sign", "ocr", "edit existing text", "pages"):
        assert claim in text, claim
    assert root.findtext("project_license") == "AGPL-3.0-or-later"
    assert root.find("launchable[@type='desktop-id']").text == f"{APP_ID}.desktop"
    assert root.find("provides/binary").text == "leaf"
    assert root.find("provides/mediatype").text == "application/pdf"
    assert {c.text for c in root.findall("categories/category")} >= {"Office"}
    assert root.find("content_rating").get("type") == "oars-1.1"
    assert root.find("url[@type='homepage']").text.startswith("https://")
    versions = [r.get("version") for r in root.findall("releases/release")]
    assert _version() in versions
    shots = root.findall("screenshots/screenshot")
    assert shots and sum(s.get("type") == "default" for s in shots) == 1
    for s in shots:
        assert s.findtext("caption")
        url = s.find("image").text
        assert url.startswith("https://") and "marketing/out/screens/light/" in url and url.endswith("1920x1080.png")
    assert root.find("developer") is not None


def test_metainfo_screenshots_match_scene_names():
    """Screenshot URLs name files shoot_all.py produces (NN-<scene>-1920x1080.png)."""
    scenes = _read("marketing", "shots", "scenes.py")
    names = dict((int(n), s) for n, s in re.findall(r'@scene\((\d+), "([^"]+)"\)', scenes))
    root = ET.parse(os.path.join(LINUX, f"{APP_ID}.metainfo.xml")).getroot()
    for img in root.findall("screenshots/screenshot/image"):
        m = re.search(r"/(\d\d)-([a-z0-9-]+)-1920x1080\.png$", img.text)
        assert m and names.get(int(m.group(1))) == m.group(2), img.text


def test_desktop_entry():
    path = os.path.join(LINUX, f"{APP_ID}.desktop")
    cp = configparser.ConfigParser(interpolation=None, strict=True)
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    e = cp["Desktop Entry"]
    assert e["Type"] == "Application" and e["Name"] == "Leaf"
    assert e["Exec"].split()[0] == "leaf" and "%F" in e["Exec"]
    assert e["Icon"] == APP_ID
    assert e["Terminal"] == "false"
    assert "application/pdf;" in e["MimeType"]
    assert e["Categories"].endswith(";") and "Office" in e["Categories"]
    assert e["Keywords"].endswith(";") and "PDF" in e["Keywords"]
    assert e["StartupWMClass"] == "leaf"
    actions = [a for a in e["Actions"].split(";") if a]
    assert actions == ["new-window"]
    for a in actions:
        sec = cp[f"Desktop Action {a}"]
        assert sec["Name"] and sec["Exec"].startswith("leaf ")
    assert not os.path.exists(os.path.join(PKG, "leaf.desktop"))  # moved to packaging/linux/<app-id>.desktop


def test_spec_has_console_cli_exe():
    spec = _read("packaging", "leaf.spec")
    m = re.search(r'EXE\((?:[^()]|\([^()]*\))*name="leaf-cli"(?:[^()]|\([^()]*\))*\)', spec)
    assert m, "leaf-cli EXE missing"
    assert "console=True" in m.group(0) and "exclude_binaries=True" in m.group(0)
    assert "COLLECT(*_exes" in spec  # same COLLECT: one folder, shared _internal/
    assert "_docs" in spec and '"docs"' in spec


def test_workflow_parses_and_has_packaging_steps():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(_read(".github", "workflows", "build.yml"))
    jobs = wf["jobs"]
    meta = " ".join(s.get("run", "") for s in jobs["metadata"]["steps"])
    assert "packaging/linux/validate.sh" in meta and "appstream" in meta
    steps = jobs["build"]["steps"]
    runs = " ".join(s.get("run", "") for s in steps)
    assert "build_msix.ps1" in runs and "leaf-cli.exe" in runs
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact") and "if" not in s)
    paths = upload["with"]["path"]
    for pat in ("dist/*.zip", "dist/*.exe", "dist/*.msix", "dist/*.dmg", "dist/*.tar.gz", "dist/*.AppImage"):
        assert pat in paths
    assert "metadata" in jobs["release"]["needs"]
    assert "\b" not in _read(".github", "workflows", "build.yml")


def test_workflow_token_is_read_only_by_default():
    """Only the release job may write (draft releases); builds of pull requests run untrusted code."""
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(_read(".github", "workflows", "build.yml"))
    assert wf["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in (wf.get("on") or wf.get(True))
    for name, job in wf["jobs"].items():
        assert name == "release" or "permissions" not in job, name
        for s in job["steps"]:
            if s.get("uses", "").startswith("actions/checkout"):
                assert s["with"]["persist-credentials"] is False, name


def test_installer_dir_follows_install_mode():
    """{autopf}: the user's Programs folder per user, Program Files when installed for all users. A machine-wide
    install (HKLM file association) must not run Leaf.exe from a folder the installing user can change."""
    iss = _read("packaging", "leaf.iss")
    assert "DefaultDirName={autopf}\\Leaf" in iss and "{localappdata}" not in iss


def test_validate_script_reads_app_id():
    sh = _read("packaging", "linux", "validate.sh")
    assert "appstreamcli validate --no-net" in sh


def test_flatpak_manifest():
    yaml = pytest.importorskip("yaml")
    path = os.path.join(PKG, "flatpak", f"{APP_ID}.yml")
    m = yaml.safe_load(_read(path))
    assert m["id"] == APP_ID and m["command"] == "leaf"
    assert m["runtime"] == "org.freedesktop.Platform" and m["sdk"] == "org.freedesktop.Sdk"
    fa = m["finish-args"]
    for arg in ("--socket=wayland", "--socket=fallback-x11", "--share=ipc", "--share=network"):
        assert arg in fa
    assert "--socket=x11" not in fa
    assert any(a.startswith("--filesystem=") for a in fa)
    mod = m["modules"][0]
    archive = next(s for s in mod["sources"] if s["type"] == "archive")
    assert archive["url"].endswith(f"leaf-{_version()}-linux-x86_64.tar.gz")
    assert isinstance(archive["sha256"], str) and len(archive["sha256"]) == 64  # quoted: YAML would read digits as int
    cmds = "\n".join(mod["build-commands"])
    assert "/app/share/metainfo/${FLATPAK_ID}.metainfo.xml" in cmds
    assert "/app/share/applications/${FLATPAK_ID}.desktop" in cmds and "/app/bin/leaf" in cmds


def test_linux_build_ships_metadata():
    sh = _read("packaging", "build_linux.sh")
    assert "packaging/leaf.desktop" not in sh
    assert '"$METAINFO"' in sh and "icons/hicolor" in sh and "usr/share/metainfo" in sh


def _pkgbuild_vars(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"^([a-z_0-9]+)=(\([^)]*\)|\S+)", text, re.M):
        out[m.group(1)] = m.group(2)
    return out


def test_pkgbuild_fields():
    text = _read("packaging", "aur", "PKGBUILD")
    v = _pkgbuild_vars(text)
    for key in ("pkgname", "pkgver", "pkgrel", "pkgdesc", "arch", "url", "license", "depends", "source_x86_64",
                "sha256sums_x86_64", "options"):
        assert key in v, key
    assert v["pkgname"] == "leaf-pdf-bin" and v["pkgver"] == _version()
    assert v["_appid"] == APP_ID
    for dep in ("glibc", "libglvnd", "libxkbcommon", "fontconfig", "dbus", "xcb-util-cursor"):
        assert f"'{dep}'" in v["depends"], dep
    assert "!strip" in v["options"]
    assert "package()" in text and "usr/share/metainfo" in text and "usr/share/applications" in text
    assert "hicolor" in text
    srcinfo = _read("packaging", "aur", ".SRCINFO")
    assert f"pkgver = {_version()}" in srcinfo and "pkgname = leaf-pdf-bin" in srcinfo
    for dep in ("glibc", "libglvnd", "xcb-util-cursor"):
        assert f"depends = {dep}\n" in srcinfo


def test_msix_manifest():
    ns = {
        "m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
        "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
        "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
    }
    root = ET.parse(os.path.join(PKG, "msix", "AppxManifest.xml")).getroot()
    ident = root.find("m:Identity", ns)
    assert ident.get("Name") == "{{IDENTITY_NAME}}" and ident.get("Publisher") == "{{PUBLISHER}}"
    assert ident.get("Version") == "{{VERSION}}" and ident.get("ProcessorArchitecture") == "x64"
    apps = root.findall("m:Applications/m:Application", ns)
    gui = apps[0]
    assert gui.get("Executable") == "Leaf.exe" and gui.get("EntryPoint") == "Windows.FullTrustApplication"
    assert any(a.get("Executable") == "leaf-cli.exe" for a in apps)
    ft = gui.find(".//uap:FileTypeAssociation/uap:SupportedFileTypes/uap:FileType", ns)
    assert ft.text == ".pdf"
    assert root.find("m:Capabilities/rescap:Capability", ns).get("Name") == "runFullTrust"
    ve = gui.find("uap:VisualElements", ns)
    referenced = {ve.get("Square150x150Logo"), ve.get("Square44x44Logo"), root.findtext("m:Properties/m:Logo", namespaces=ns)}
    tile = ve.find("uap:DefaultTile", ns)
    referenced |= {tile.get("Wide310x150Logo"), tile.get("Square310x310Logo"), tile.get("Square71x71Logo")}
    src = _read("packaging", "msix", "make_assets.py")
    names = set(re.findall(r'"(\w+)": \(\d+, \d+', src))
    assert {r.replace("Assets\\", "").removesuffix(".png") for r in referenced} <= names


def test_msix_assets_render(app, tmp_path):  # noqa: F811 - pytest fixture
    mod = _load("leaf_msix_assets", os.path.join(PKG, "msix", "make_assets.py"))
    files = mod.main(str(tmp_path))
    from PySide6.QtGui import QImage

    img = QImage(str(tmp_path / "Wide310x150Logo.scale-200.png"))
    assert (img.width(), img.height()) == (620, 300)
    assert QImage(str(tmp_path / "Square44x44Logo.targetsize-256_altform-unplated.png")).width() == 256
    assert QImage(str(tmp_path / "StoreLogo.png")).pixelColor(25, 25).alpha() == 255  # icon drawn, not empty
    # plated assets are full-bleed (a transparent margin is shown as a blue accent frame by Windows and the Store);
    # only the unplated taskbar/Start icons keep transparent corners
    for name in ("StoreLogo.png", "Square150x150Logo.scale-200.png", "Square44x44Logo.targetsize-48.png"):
        corner = QImage(str(tmp_path / name)).pixelColor(0, 0)
        assert corner.alpha() == 255 and corner.green() > corner.blue(), name
    assert QImage(str(tmp_path / "Square44x44Logo.targetsize-48_altform-unplated.png")).pixelColor(0, 0).alpha() == 0
    manifest = _read("packaging", "msix", "AppxManifest.xml")
    assert manifest.count(f'BackgroundColor="{mod.PLATE}"') == 2  # the plate color matches the assets
    assert len(files) == 2 * 6 + 2 * len(mod.TARGET_SIZES)


def test_set_app_id_dry_run(capsys):
    mod = _load("leaf_set_app_id", os.path.join(PKG, "set_app_id.py"))
    assert mod.default_id("my-org") == "io.github.my_org.Leaf"
    assert mod.default_id("9lives") == "io.github._9lives.Leaf"
    assert mod.main(["some-owner", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "io.github.some_owner.Leaf" in out and "(dry run)" in out
    assert f"{APP_ID}.metainfo.xml" in out  # would be renamed
    assert _app_id() == APP_ID  # dry run changed nothing



def test_version_is_consistent():
    """One version everywhere: pyproject, leaf.__version__, the newest AppStream release, Inno Setup, AUR."""
    import re

    from leaf import __version__

    assert re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M).group(1) == __version__
    meta = ET.parse(os.path.join(LINUX, f"{APP_ID}.metainfo.xml")).getroot()
    assert meta.find("releases/release").get("version") == __version__
    assert f'#define Version "{__version__}"' in _read("packaging", "leaf.iss")
    assert f"pkgver={__version__}\n" in _read("packaging", "aur", "PKGBUILD")
