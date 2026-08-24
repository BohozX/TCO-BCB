from __future__ import annotations

import sys

import main as tco


def main() -> int:
    print("=" * 78)
    print("TCO-BCB  |  construccion inicial de las bases")
    print("=" * 78)
    codigo = tco.ejecutar(reconstruir=True)
    print("\n[OK] Listo. A partir de ahora usar: python codigo/main.py" if codigo == 0
          else "\n[ERROR] La construccion inicial termino con incidencias.")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
