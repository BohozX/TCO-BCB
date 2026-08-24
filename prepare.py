"""
TCO-BCB — construccion inicial de las bases (se ejecuta una sola vez).

    python prepare.py

El repositorio nacio sin archivos historicos previos, asi que la base se construye
directamente desde las fuentes oficiales del BCB: el PDF anual de cotizaciones y el
CSV de operaciones por banco y tipo de cambio.

Si en el futuro se coloca un CSV historico propio en data/ (por ejemplo un respaldo
exportado a mano), este script lo detecta, informa su estructura real —encoding,
separador, decimales, formato de fechas y encabezados— y NO lo sobrescribe en
silencio: primero muestra que contiene para poder decidir.

La reconstruccion borra tco.csv, bancos.csv y detalle.csv y los vuelve a generar desde
cero. data/raw.zip se conserva y solo se completa: es el archivo de auditoria.
"""

from __future__ import annotations

import csv
import glob
import os
import sys

import main as tco

CANONICOS = {os.path.basename(p) for p in (tco.CSV_TCO, tco.CSV_BANCOS, tco.CSV_DETALLE)}


def inspeccionar(ruta: str) -> None:
    """Informa la estructura real del archivo, sin suponer nada."""
    with open(ruta, "rb") as fh:
        crudo = fh.read()

    encoding = "utf-8-sig" if crudo.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        texto = crudo.decode(encoding)
    except UnicodeDecodeError:
        encoding = "latin-1"
        texto = crudo.decode(encoding)

    lineas = texto.splitlines()
    muestra = "\n".join(lineas[:20])
    try:
        sep = csv.Sniffer().sniff(muestra, delimiters=";,\t|").delimiter
    except csv.Error:
        sep = max(";,\t|", key=lambda c: muestra.count(c))

    filas = list(csv.reader(lineas, delimiter=sep))
    encabezado = filas[0] if filas else []
    cuerpo = filas[1:]

    print(f"\n  archivo    : {os.path.relpath(ruta, tco.RAIZ)}")
    print(f"  bytes      : {len(crudo)}")
    print(f"  encoding   : {encoding}")
    print(f"  separador  : {sep!r}")
    print(f"  filas      : {len(cuerpo)}")
    print(f"  encabezados: {encabezado}")
    if cuerpo:
        print(f"  primera    : {cuerpo[0]}")
        print(f"  ultima     : {cuerpo[-1]}")
    coma = sum(1 for f in cuerpo[:200] for c in f if "," in c and c.replace(",", "").replace(".", "").isdigit())
    print(f"  decimales  : {'coma (formato boliviano)' if coma else 'punto o sin decimales'}")


def main() -> int:
    print("=" * 78)
    print("TCO-BCB  |  construccion inicial de las bases")
    print("=" * 78)

    ajenos = [p for p in sorted(glob.glob(os.path.join(tco.DATA, "*.csv")))
              if os.path.basename(p) not in CANONICOS]
    if ajenos:
        print(f"\n[..] Se encontraron {len(ajenos)} CSV historicos en data/ que no genera este "
              f"proyecto.\n     Se informa su estructura real y se dejan intactos:")
        for ruta in ajenos:
            inspeccionar(ruta)
        print("\n[AVISO] Estos archivos NO se usan como fuente ni se modifican. Las bases se "
              "construyen\n        desde las fuentes oficiales del BCB, que son verificables.")
    else:
        print("\n[..] No hay CSV historicos previos en data/; se construye todo desde el BCB.")

    print("\n[..] Reconstruyendo tco.csv, bancos.csv y detalle.csv desde las fuentes oficiales...\n")
    codigo = tco.ejecutar(reconstruir=True)

    if codigo == 0:
        print("\n[OK] Base inicial construida. A partir de ahora usar: python main.py")
    else:
        print("\n[ERROR] La construccion inicial termino con incidencias (ver arriba).")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
