"""Écrit le numéro de version (ex. « v0.2.0 » ou « 0.2.0 ») dans autocut/__init__.py. Utilisé par la CI sur les tags."""

import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("v")
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    sys.exit(f"Version invalide : {sys.argv[1]!r}. Utilise un tag de la forme v0.2.0")
init = Path(__file__).resolve().parent.parent / "autocut" / "__init__.py"
init.write_text(re.sub(r'__version__ = "[^"]*"', f'__version__ = "{version}"', init.read_text(encoding="utf-8")), encoding="utf-8")
print("Version", version)
