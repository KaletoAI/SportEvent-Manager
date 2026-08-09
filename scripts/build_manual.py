#!/usr/bin/env python3
"""Aus den .src.html-Quellen unter docs/ eigenständige HTML-Dateien bauen.

Jede `docs/<name>.src.html` wird zu `app/help/<name>.html`, wobei die
Screenshots als data:-URI eingebettet werden. Die Ergebnisse lassen sich
ohne Bilderordner weitergeben und im Browser direkt als PDF drucken
(„Drucken → Als PDF sichern“).

Ausgabeort ist `app/help/`, weil das Docker-Image nur `app/` kopiert — nur
von dort kann die App die Dokumente unter /hilfe ausliefern. Bewusst NICHT
`app/static/`: das Verzeichnis ist öffentlich gemountet, das Handbuch ist
aber nur für Angemeldete.

    source .venv/bin/activate
    python scripts/build_manual.py            # alle Dokumente
    python scripts/build_manual.py kurzreferenz   # nur eines

Die Bilder werden dabei auf eine adaptive Palette reduziert — bei
UI-Screenshots ist das optisch nicht zu unterscheiden, spart aber rund
zwei Drittel der Dateigröße.
"""

import base64
import io
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT_DIR = ROOT / "app" / "help"

# Farbtiefe der eingebetteten PNGs. 256 reicht für die flächigen
# UI-Farben; darunter franst der Text sichtbar aus.
COLORS = 256


def encode(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    img = img.quantize(colors=COLORS, method=Image.MEDIANCUT)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build(src: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / src.name.replace(".src.html", ".html")
    html = src.read_text(encoding="utf-8")
    missing, embedded, raw, packed = [], 0, 0, 0

    def replace(match: re.Match) -> str:
        nonlocal embedded, raw, packed
        rel = match.group(1)
        path = (src.parent / rel).resolve()
        if not path.exists():
            missing.append(rel)
            return match.group(0)
        raw += path.stat().st_size
        data = encode(path)
        packed += len(data)
        embedded += 1
        return f'src="data:image/png;base64,{data}"'

    html = re.sub(r'src="((?:screenshots/)[^"]+)"', replace, html)

    if missing:
        print(f"{src.name}: fehlende Bilder:", *missing, sep="\n  ")
        sys.exit(1)

    out.write_text(html, encoding="utf-8")
    print(f"{out.name}: {embedded} Bilder "
          f"({raw / 1e6:.1f} MB → {packed * 3 / 4 / 1e6:.1f} MB), "
          f"Datei {out.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    wanted = sys.argv[1:]
    sources = sorted(DOCS.glob("*.src.html"))
    if wanted:
        sources = [s for s in sources
                   if s.name.replace(".src.html", "") in wanted]
        if not sources:
            raise SystemExit(f"Keine Quelle passend zu: {', '.join(wanted)}")
    if not sources:
        raise SystemExit(f"Keine .src.html-Dateien in {DOCS}")

    for src in sources:
        build(src)
    print("Als PDF: Datei im Browser öffnen → Drucken → Als PDF sichern")


if __name__ == "__main__":
    main()
