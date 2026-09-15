"""Проверка: APP_VERSION в gui.py совпадает с тегом релиза.

Использование: python scripts/check_version.py v1.2.3
Код возврата 0 — совпадает (CI продолжает сборку), 1 — нет.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    tag = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("v")
    src = Path(__file__).resolve().parent.parent / "gui.py"
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src.read_text(encoding="utf-8"))
    gui_version = m.group(1) if m else None
    ok = bool(gui_version and gui_version == tag)
    print(f"gui={gui_version} tag={tag} -> {'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
