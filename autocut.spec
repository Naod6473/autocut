# Configuration PyInstaller : produit dist/Autocut.exe (un seul fichier, sans console).
# La bibliothèque libsndfile de soundfile est récupérée par le hook de pyinstaller-hooks-contrib.
a = Analysis(
    ["run_autocut.py"],
    hiddenimports=["soxr"],
    excludes=["tkinter", "matplotlib", "scipy", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Autocut",
    console=False,
    upx=False,
)
