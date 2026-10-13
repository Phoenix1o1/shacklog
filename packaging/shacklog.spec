# PyInstaller spec for ShackLog — one self-contained executable per OS.
#
# Build from the project root:
#     pyinstaller packaging/shacklog.spec --noconfirm
# Output: dist/ShackLog(.exe)
#
# PyInstaller cannot cross-compile: build the Windows .exe on Windows, the macOS binary
# on a Mac, the Linux one on Linux (the GitHub Actions workflow does all three).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is provided by PyInstaller)
WEB = ROOT / "shacklog" / "web"

# Views are imported by name at runtime (importlib), so list every module explicitly.
# Walk the source tree instead of collect_submodules("shacklog"): that helper runs in a
# subprocess where the project isn't importable and silently returns too little.
own_modules = sorted(
    ".".join(p.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
    for p in (ROOT / "shacklog").rglob("*.py")
)
hidden = own_modules + collect_submodules("waitress") + [
    "sqlalchemy.dialects.sqlite",
    "platformdirs",
    "pypdf",
    "markdown",
    "markdown.extensions.tables",
    "markdown.extensions.fenced_code",
    "markdown.extensions.sane_lists",
    "markdown.extensions.toc",
]
if sys.platform == "win32":
    # Tray icon for background mode (shacklog/tray.py).
    hidden += ["pystray", "pystray._win32", "PIL.Image", "PIL.ImageDraw"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(WEB / "templates"), "shacklog/web/templates"),
        (str(WEB / "static"), "shacklog/web/static"),
        (str(ROOT / "shacklog" / "help"), "shacklog/help"),
    ],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ShackLog",
    # Windows: no console window; ShackLog runs in the background with a tray icon
    # (shacklog/tray.py). Linux/macOS: started from a terminal, which shows the address.
    console=sys.platform != "win32",
    upx=False,
    icon=str(ROOT / "packaging" / "shacklog.ico") if (ROOT / "packaging" / "shacklog.ico").exists() else None,
)
