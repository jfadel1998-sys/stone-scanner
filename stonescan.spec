# PyInstaller spec for the Stone Scanner desktop app (onedir). Cross-platform:
#   Windows:  .\.venv\Scripts\pyinstaller.exe stonescan.spec --noconfirm   (then build_exe.ps1)
#   macOS:    ./.venv/bin/pyinstaller stonescan.spec --noconfirm           (then build_mac.sh)
# Chromium is NOT bundled here; it is copied into dist/StoneScanner/browsers by the
# platform build script and located at runtime via PLAYWRIGHT_BROWSERS_PATH.
#
# The native window uses different backends per OS: WebView2 via pythonnet/.NET on
# Windows, Cocoa/WKWebView via PyObjC on macOS. Those deps are gated by sys.platform
# so the same spec builds on both (and neither package need be installed on the other).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
gn_datas, gn_binaries, gn_hidden = collect_all("geonamescache")  # offline city coords for the map
wv_datas, wv_binaries, wv_hidden = collect_all("webview")       # pywebview (native window)
ort_datas, ort_binaries, ort_hidden = collect_all("onnxruntime")  # CLIP image search (search-by-photo)

# Catalog providers are resolved by NAME at runtime (providers/__init__.py calls
# import_module(f".{name}")), which static analysis cannot follow — without this a provider
# is silently absent from the frozen app and only that platform's suppliers stop refreshing,
# with no error in dev. Enumerate the directory rather than collect_submodules(), which has
# to import the package and silently yields nothing here because pathex is empty.
_provider_dir = Path(SPECPATH) / "stonescan" / "providers"
provider_hidden = [f"stonescan.providers.{p.stem}" for p in sorted(_provider_dir.glob("*.py"))
                   if p.stem != "__init__"]
assert len(provider_hidden) >= 6, f"providers not found for freezing: {provider_hidden}"

# Cross-platform base collections (backend-agnostic).
base_hidden = pw_hidden + wv_hidden + gn_hidden + ort_hidden + provider_hidden + [
    # search-by-photo (imagesearch.py imports these lazily, so name them explicitly)
    "onnxruntime", "PIL", "PIL.Image", "numpy",
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    "multipart", "python_multipart",  # FastAPI Form() parsing (sourcing lists)
    # Providers are resolved by name at runtime (importlib), so PyInstaller's static
    # analysis can't see them — without these, a refresh in the packaged app dies with
    # ModuleNotFoundError on the first non-StoneProfits supplier.
    "stonescan.providers.umi", "stonescan.providers.slabware",
    "stonescan.providers.stonetrash", "stonescan.providers.slabcloud",
]
base_datas = pw_datas + wv_datas + gn_datas + ort_datas + [
    ("stonescan/web/templates", "stonescan/web/templates"),
    ("stonescan/web/static", "stonescan/web/static"),
    ("stonescan/data/us_zips.json.gz", "stonescan/data"),  # offline zip->coords for proximity
    ("stonescan/models/clip/clip_vision.onnx", "stonescan/models/clip"),  # search-by-photo model
    ("suppliers.json", "seed"),
    ("locations.json", "seed"),
    ("stonescan.db", "seed"),
]
base_binaries = pw_binaries + wv_binaries + gn_binaries + ort_binaries

if sys.platform == "win32":
    # .NET / WebView2 bridge — Windows only.
    cl_datas, cl_binaries, cl_hidden = collect_all("clr_loader")
    pn_datas, pn_binaries, pn_hidden = collect_all("pythonnet")
    hiddenimports = base_hidden + cl_hidden + pn_hidden + ["clr", "webview.platforms.winforms"]
    datas = base_datas + cl_datas + pn_datas
    binaries = base_binaries + cl_binaries + pn_binaries
elif sys.platform == "darwin":
    # Cocoa / WKWebView backend via PyObjC — macOS only. collect_submodules covers
    # pyobjc's lazily-generated framework modules that static analysis misses (the
    # classic "webview backend not found" failure in a frozen mac app).
    extra_hidden = ["webview.platforms.cocoa"]
    for _fw in ("objc", "Foundation", "AppKit", "WebKit", "Quartz", "Security", "PyObjCTools"):
        try:  # skip any framework not installed as a separate submodule package
            extra_hidden += collect_submodules(_fw)
        except Exception:
            pass
    hiddenimports = base_hidden + extra_hidden
    datas = base_datas
    binaries = base_binaries
else:  # linux / other — server + system-browser fallback only
    hiddenimports, datas, binaries = base_hidden, base_datas, base_binaries

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StoneScanner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StoneScanner",
)
