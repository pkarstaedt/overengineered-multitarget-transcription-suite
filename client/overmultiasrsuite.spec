# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for OverMultiASRSuite
# Build with:  pyinstaller overmultiasrsuite.spec

a = Analysis(
    ["overmultiasrsuite.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pystray picks the backend at runtime — pin the Win32 one
        "pystray._win32",
        # Settings UI is imported lazily, so include tkinter explicitly
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "_tkinter",
        # PIL may be imported lazily by pystray
        "PIL._imagingtk",
        "PIL.ImageTk",
        # sounddevice loads its PortAudio wrapper at runtime
        "sounddevice",
        # keyboard uses ctypes — no hidden imports needed, but list it anyway
        "keyboard",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Cut unused heavy packages to keep the build smaller
        "matplotlib", "scipy", "pandas", "IPython", "jupyter",
        "tkinter.test",   # keep tkinter itself, drop test suite
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,                     # one-file: bundle everything into the exe
    a.zipfiles,
    a.datas,
    [],
    name="OverMultiASRSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX can break some DLLs; leave off by default
    upx_exclude=[],
    runtime_tmpdir=None,            # extract to %TEMP%\\_MEIxxxxx on each run
    console=False,                  # no black console window on launch
    uac_admin=True,                 # embed UAC manifest → always runs as admin
    icon=None,                      # set to "icon.ico" if you add one
)
