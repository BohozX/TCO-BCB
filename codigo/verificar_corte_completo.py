"""Salir sin reprocesar cuando el corte nocturno del BCB ya esta completo."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import zipfile
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[1]
DATOS = RAIZ / "datos"
HUSO_BOLIVIA = dt.timezone(dt.timedelta(hours=-4))


def ultimo_corte(ruta: Path) -> str:
    """Devolver la fecha de corte ISO mas reciente de un CSV."""
    if not ruta.is_file():
        return ""
    with ruta.open("r", encoding="utf-8-sig", newline="") as archivo:
        fechas = {
            (fila.get("fecha_corte") or "").strip()
            for fila in csv.DictReader(archivo)
            if (fila.get("fecha_corte") or "").strip()
        }
    validas = []
    for fecha in fechas:
        try:
            dt.date.fromisoformat(fecha)
        except ValueError:
            continue
        validas.append(fecha)
    return max(validas, default="")


def raw_contiene_corte(ruta: Path, fecha: str) -> bool:
    """Comprobar que raw.zip conserve el CSV publico diario y su manifiesto."""
    if not ruta.is_file():
        return False
    try:
        with zipfile.ZipFile(ruta) as archivo:
            manifiesto = json.loads(archivo.read("manifest.json"))
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile, OSError):
        return False
    return f"operaciones/{fecha}.csv" in manifiesto


def corte_completo(fecha: str, datos: Path = DATOS) -> tuple[bool, dict[str, object]]:
    """Exigir el mismo corte en los tres CSV y en el paquete RAW."""
    estado: dict[str, object] = {
        "objetivo": fecha,
        "tco": ultimo_corte(datos / "tco.csv"),
        "bancos": ultimo_corte(datos / "bancos.csv"),
        "detalle": ultimo_corte(datos / "detalle.csv"),
        "raw": raw_contiene_corte(datos / "raw.zip", fecha),
    }
    completo = all(estado[nombre] == fecha for nombre in ("tco", "bancos", "detalle"))
    completo = completo and bool(estado["raw"])
    return completo, estado


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fecha",
        help="Fecha de corte Bolivia en formato AAAA-MM-DD; por defecto usa hoy.",
    )
    args = parser.parse_args()
    fecha = args.fecha or dt.datetime.now(HUSO_BOLIVIA).date().isoformat()
    dt.date.fromisoformat(fecha)

    completo, estado = corte_completo(fecha)
    print(json.dumps(estado, ensure_ascii=False, sort_keys=True))
    print("CORTE_COMPLETO" if completo else "CORTE_INCOMPLETO")
    return 0 if completo else 1


if __name__ == "__main__":
    raise SystemExit(main())
