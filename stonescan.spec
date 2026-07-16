# PyInstaller spec for the Stone Scanner desktop app (onedir).
# Build:  .\.venv\Scripts\pyinstaller.exe stonescan.spec --noconfirm
# Chromium is NOT bundled here; it is copied into dist/StoneScanner/browsers
# by build_exe.ps1 and located at runtime via PLAYWRIGHT_BROWSERS_PATH.

from PyInstaller.utils.hooks import collect_all

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
gn_datas, gn_binaries, gn_hidden = collect_all("geonamescache")  # offline city coords for the map
wv_datas, wv_binaries, wv_hidden = collect_all("webview")       # pywebview (native window)
cl_datas, cl_binaries, cl_hidden = collect_all("clr_loader")    # .NET loader
pn_datas, pn_binaries, pn_hidden = collect_all("pythonnet")     # Python.NET (WebView2 bridge)

hiddenimports = pw_hidden + wv_hidden + cl_hidden + pn_hidden + gn_hidden + [
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    "clr", "webview.platforms.winforms",
    "multipart", "python_multipart",  # FastAPI Form() parsing (sourcing lists)
]

datas = pw_datas + wv_datas + cl_datas + pn_datas + gn_datas + [
    ("stonescan/web/templates", "stonescan/web/templates"),
    ("stonescan/web/static", "stonescan/web/static"),
    ("suppliers.json", "seed"),
    ("locations.json", "seed"),
    ("stonescan.db", "seed"),
]
binaries = pw_binaries + wv_binaries + cl_binaries + pn_binaries + gn_binaries

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
