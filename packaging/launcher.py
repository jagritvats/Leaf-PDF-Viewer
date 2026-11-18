"""Entry script for packaged builds (PyInstaller needs a file, not a module)."""
import os
import sys


def _ssl_certs() -> None:
    """Frozen macOS/Linux builds: the bundled OpenSSL looks for CA certificates where the build machine's Python
    had them. If that location doesn't exist here, point it at certifi's bundle so HTTPS (a timestamp server for
    digital signatures, via aiohttp) can verify servers. Windows uses the system store; never overrides the user."""
    if not getattr(sys, "frozen", False) or sys.platform == "win32" or os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import ssl

        p = ssl.get_default_verify_paths()
        if (p.cafile and os.path.isfile(p.cafile)) or (p.capath and os.path.isdir(p.capath) and os.listdir(p.capath)):
            return
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:  # noqa: BLE001 - best effort; signing without a timestamp server needs no network
        pass


_ssl_certs()

from leaf.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
