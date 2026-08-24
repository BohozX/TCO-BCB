"""
TCO-BCB — recopilacion diaria del Tipo de Cambio Oficial del Banco Central de Bolivia.

Ejecutar:   python main.py

Fuentes oficiales (URL limpias, sin parametros de rastreo):
  * PDF anual de cotizaciones oficiales   -> tiposDeCambioHistorico/pdf.php?anio=AAAA
  * Tabla anual en HTML (solo se archiva) -> tiposDeCambioHistorico/index.php?anio=AAAA
  * CSV de operaciones por banco y TC     -> tco_tcreferencial_descargar_csv.php?desde=&hasta=

Principio de fidelidad: los CSV guardan el valor EXACTAMENTE como lo publica el BCB.
Lo unico que se cambia es el separador decimal (coma -> punto) y se quita el separador
de miles, para que el archivo sea legible por cualquier herramienta. No se redondea,
no se recalcula y no se rellena ningun valor ausente.

Decision metodologica documentada (ver README): el PDF anual es la fuente autoritativa
de la serie de cotizaciones porque distingue explicitamente VENTA/COMPRA (regimen hasta
junio 2026) de OFICIAL (regimen posterior). Las vistas HTML/XLS/ODS reproducen un formato
heredado de dos columnas y fabrican una "venta" = oficial + 0,10 para el periodo posterior
a la reforma; por eso NO se usan como fuente de datos, solo se archivan como evidencia.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

# --------------------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------------------

RAIZ = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(RAIZ, "data")
STATUS = os.path.join(RAIZ, "status")

CSV_TCO = os.path.join(DATA, "tco.csv")
CSV_BANCOS = os.path.join(DATA, "bancos.csv")
CSV_DETALLE = os.path.join(DATA, "detalle.csv")
RAW_ZIP = os.path.join(DATA, "raw.zip")
PNG_STATUS = os.path.join(STATUS, "status.png")

BASE = "https://www.bcb.gob.bo/"
URL_SERIE_TCO = BASE + "?q=content/tipo-de-cambio-oficial-del-d%C3%B3lar-estadounidense-serie-de-tiempo"
URL_HIST_HTML = BASE + "tiposDeCambioHistorico/index.php?anio={anio}"
URL_HIST_PDF = BASE + "tiposDeCambioHistorico/pdf.php?anio={anio}"
URL_OPERACIONES = BASE + "tco_tcreferencial_descargar_csv.php?desde={desde}&hasta={hasta}"

# Primer dia de interes de la serie de cotizaciones (el cambio metodologico ocurre en junio 2026).
FECHA_INICIO = dt.date(2026, 6, 1)
# Primer corte con reporte de operaciones publicado por el BCB.
PRIMER_CORTE = dt.date(2026, 6, 26)

TOTAL_BANCOS = "TOTAL BANCOS"
HUSO_BOLIVIA = dt.timezone(dt.timedelta(hours=-4))

# Tolerancias derivadas empiricamente de la propia fuente (ver README):
TOL_TCO = 1e-4            # CSV vs PDF: solo error de representacion decimal
TOL_REDONDEO_CELDA = 0.5  # el BCB publica cada Monto redondeado a USD entero
TOL_PONDERADO = 0.005     # el TCO publicado es la media ponderada llevada a 2 decimales

MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
    "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; TCO-BCB/1.0; +https://github.com/BohozX/TCO-BCB)"}

ERRORES: list[str] = []
AVISOS: list[str] = []


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def error(msg: str) -> None:
    print(f"[ERROR] {msg}")
    ERRORES.append(msg)


def aviso(msg: str) -> None:
    print(f"[AVISO] {msg}")
    AVISOS.append(msg)


# --------------------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------------------

def descargar(url: str, intentos: int = 3) -> bytes:
    ultimo = None
    for i in range(intentos):
        try:
            r = requests.get(url, headers=UA, timeout=90)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if i + 1 < intentos:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"No se pudo descargar {url}: {type(ultimo).__name__}: {ultimo}")


# --------------------------------------------------------------------------------------
# Valores: se conserva el literal del BCB; el float es solo para validar
# --------------------------------------------------------------------------------------

def literal(txt: str) -> str:
    """'1.234,56' -> '1234.56' ; '9,6300' -> '9.6300' ; '' y '-' -> ''. Sin redondear."""
    s = (txt or "").strip()
    if s in ("", "-"):
        return ""
    return s.replace(".", "").replace(",", ".")


def numero(txt: str):
    s = literal(txt)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def dias_de_vigencia(txt: str) -> list[str]:
    """La vigencia puede ser un dia ('2026-08-11') o un bloque de feriados
    ('2026-08-06 al 2026-08-10'). Devuelve la lista de dias que cubre.
    El literal publicado se conserva intacto en bancos.csv y detalle.csv."""
    s = (txt or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return [s]
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*al\s*(\d{4}-\d{2}-\d{2})", s, re.IGNORECASE)
    if not m:
        aviso(f"Formato de vigencia no reconocido: {s!r}")
        return []
    ini, fin = dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))
    if fin < ini:
        error(f"Rango de vigencia invertido: {s!r}")
        return []
    return [(ini + dt.timedelta(days=i)).isoformat() for i in range((fin - ini).days + 1)]


# --------------------------------------------------------------------------------------
# Fuente 1: CSV oficial de operaciones por banco y tipo de cambio
# --------------------------------------------------------------------------------------

def parsear_operaciones(contenido: bytes):
    """Devuelve (detalle, bancos, tco_ops, cortes, totales_por_tc)."""
    texto = contenido.decode("utf-8-sig", "replace")
    filas = list(csv.reader(texto.splitlines(), delimiter=";"))

    idx = None
    for i, f in enumerate(filas):
        if f and f[0].strip().lower().startswith("fecha de corte"):
            idx = i
            break
    if idx is None:
        raise RuntimeError("El CSV de operaciones no contiene la fila de encabezado esperada.")

    cab = filas[idx]
    # Cada banco ocupa un par de columnas (N°, Monto) a partir de la columna 3.
    columnas = [(cab[j].strip(), j) for j in range(3, len(cab), 2) if cab[j].strip()]
    if not columnas or columnas[-1][0] != TOTAL_BANCOS:
        raise RuntimeError(f"Encabezado de bancos inesperado: {[c[0] for c in columnas]}")

    datos = [f for f in filas[idx + 2:]
             if len(f) > 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[0].strip())]

    detalle, bancos, tco_ops, tot_tc = [], [], {}, {}
    cortes: list[str] = []
    vistos = set()

    for f in datos:
        corte, vigencia, etiqueta = f[0].strip(), f[1].strip(), f[2].strip()
        if corte not in vistos:
            vistos.add(corte)
            cortes.append(corte)

        for banco, j in columnas:
            cn = f[j] if j < len(f) else ""
            cm = f[j + 1] if j + 1 < len(f) else ""

            if etiqueta == "TOTAL":
                bancos.append({
                    "fecha_corte": corte, "vigencia": vigencia, "banco": banco,
                    "operaciones": literal(cn), "monto_usd": literal(cm), "tco": "",
                    "_n": numero(cn), "_m": numero(cm), "_tco": None,
                })
            elif etiqueta == "TCO":
                if banco == TOTAL_BANCOS and numero(cn) is not None:
                    # La vigencia puede cubrir varios dias (feriados): se indexa dia por dia.
                    for dia in dias_de_vigencia(vigencia):
                        tco_ops[dia] = (corte, literal(cn), numero(cn))
                for b in reversed(bancos):
                    if b["fecha_corte"] == corte and b["banco"] == banco:
                        b["tco"] = literal(cn)
                        b["_tco"] = numero(cn)
                        break
            else:
                tc_txt = literal(etiqueta)
                if not tc_txt:
                    continue
                if banco == TOTAL_BANCOS:
                    # Se guarda solo para validar; no entra en detalle.csv (es la suma).
                    tot_tc[(corte, tc_txt)] = (numero(cn), numero(cm))
                    continue
                if numero(cn) is None and numero(cm) is None:
                    continue
                if numero(etiqueta) is None:
                    aviso(f"Fila con TC no numerico ignorada: {corte} {etiqueta!r}")
                    continue
                detalle.append({
                    "fecha_corte": corte, "vigencia": vigencia, "banco": banco,
                    "tc": tc_txt, "operaciones": literal(cn), "monto_usd": literal(cm),
                    "_tc": numero(etiqueta), "_n": numero(cn), "_m": numero(cm),
                })

    return detalle, bancos, tco_ops, cortes, tot_tc


# --------------------------------------------------------------------------------------
# Fuente 2: PDF anual de cotizaciones oficiales (fuente autoritativa de la serie)
# --------------------------------------------------------------------------------------

def parsear_pdf_anual(contenido: bytes, anio: int):
    """Lee la matriz dia x mes del PDF usando coordenadas reales (sin OCR).

    Devuelve dict vigencia(ISO) -> {'tco_compra','tco_venta','tco_oficial'} con el
    literal publicado, y '_oficial' con el float para validar.
    El encabezado distingue VENTA/COMPRA (regimen antiguo) de OFICIAL (regimen nuevo);
    un mismo mes aparece dos veces cuando el cambio ocurre a mitad de mes.
    """
    import pdfplumber

    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        palabras = pdf.pages[0].extract_words()
        # Huella del contenido visible. El BCB regenera el PDF en cada descarga con un
        # timestamp y un UUID nuevos, asi que los bytes nunca se repiten; el texto si.
        huella = hashlib.sha256(
            "\n".join((p.extract_text() or "") for p in pdf.pages).encode("utf-8")
        ).hexdigest()

    lineas: list[list[dict]] = []
    for w in sorted(palabras, key=lambda w: (w["top"], w["x0"])):
        if lineas and abs(w["top"] - lineas[-1][0]["top"]) <= 3:
            lineas[-1].append(w)
        else:
            lineas.append([w])
    for ln in lineas:
        ln.sort(key=lambda w: w["x0"])

    def centro(w):
        return (w["x0"] + w["x1"]) / 2

    fila_meses = next((ln for ln in lineas if ln and ln[0]["text"].upper() == "DIAS"), None)
    if fila_meses is None:
        raise RuntimeError(f"El PDF {anio} no contiene la fila de encabezado 'DIAS'.")
    i_meses = lineas.index(fila_meses)
    fila_tipos = next((ln for ln in lineas[i_meses + 1:]
                       if any(w["text"].upper() in ("VENTA", "COMPRA", "OFICIAL") for w in ln)), None)
    if fila_tipos is None:
        raise RuntimeError(f"El PDF {anio} no contiene la fila VENTA/COMPRA/OFICIAL.")

    meses = [(MESES[w["text"].upper()], centro(w)) for w in fila_meses[1:]
             if w["text"].upper() in MESES]
    if not meses:
        raise RuntimeError(f"No se reconocio ningun mes en el encabezado del PDF {anio}.")

    # Cada subcolumna se asigna al encabezado de mes cuyo centro esta mas cerca.
    columnas = []
    for w in fila_tipos:
        clase = w["text"].upper()
        if clase in ("VENTA", "COMPRA", "OFICIAL"):
            cx = centro(w)
            columnas.append((cx, min(meses, key=lambda m: abs(m[1] - cx))[0], clase))
    if not columnas:
        raise RuntimeError(f"No se reconocio ninguna subcolumna en el PDF {anio}.")

    campo = {"VENTA": "tco_venta", "COMPRA": "tco_compra", "OFICIAL": "tco_oficial"}
    serie: dict[str, dict] = {}
    for ln in lineas[i_meses + 2:]:
        if not ln or not ln[0]["text"].strip().isdigit():
            continue  # PROM, pies de pagina, etc.
        dia = int(ln[0]["text"].strip())
        for w in ln[1:]:
            val = numero(w["text"])
            if val is None:
                continue
            cx = centro(w)
            cxc, mes, clase = min(columnas, key=lambda c: abs(c[0] - cx))
            if abs(cxc - cx) > 15:
                aviso(f"Valor del PDF {anio} sin columna clara: dia {dia} {w['text']!r}")
                continue
            try:
                fecha = dt.date(anio, mes, dia).isoformat()
            except ValueError:
                continue  # dia inexistente en ese mes
            reg = serie.setdefault(fecha, {"tco_compra": "", "tco_venta": "",
                                           "tco_oficial": "", "_oficial": None})
            c = campo[clase]
            if reg[c] and abs(float(reg[c]) - val) > TOL_TCO:
                error(f"PDF {anio}: valor inconsistente en {fecha} {c}: {reg[c]} vs {val}")
            reg[c] = literal(w["text"])
            if clase == "OFICIAL":
                reg["_oficial"] = val

    return serie, huella


# --------------------------------------------------------------------------------------
# Construccion y escritura
# --------------------------------------------------------------------------------------

def construir_tco(serie_pdf: dict, tco_ops: dict):
    """tco.csv: una fila por fecha de vigencia. Campo vacio = el BCB no lo publica."""
    filas = []
    for vigencia in sorted(serie_pdf):
        if dt.date.fromisoformat(vigencia) < FECHA_INICIO:
            continue
        reg = serie_pdf[vigencia]
        if not (reg["tco_compra"] or reg["tco_venta"] or reg["tco_oficial"]):
            continue
        filas.append({
            "fecha_corte": tco_ops.get(vigencia, ("", "", None))[0],
            "vigencia": vigencia,
            "tco_compra": reg["tco_compra"],
            "tco_venta": reg["tco_venta"],
            "tco_oficial": reg["tco_oficial"],
        })
    return filas


def escribir_csv(ruta: str, campos: list[str], filas: list[dict]) -> bool:
    """Escribe solo si el contenido cambia. Devuelve True si el archivo fue modificado."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=campos, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for f in filas:
        w.writerow({c: f.get(c, "") for c in campos})
    nuevo = buf.getvalue().encode("utf-8")

    anterior = None
    if os.path.exists(ruta):
        with open(ruta, "rb") as fh:
            anterior = fh.read()
    if anterior == nuevo:
        return False
    with open(ruta, "wb") as fh:
        fh.write(nuevo)
    return True


def leer_ultima_fecha(ruta: str, columna: str):
    if not os.path.exists(ruta):
        return None
    with open(ruta, "r", encoding="utf-8", newline="") as fh:
        valores = [f[columna] for f in csv.DictReader(fh) if f.get(columna)]
    return max(valores) if valores else None


# --------------------------------------------------------------------------------------
# Validaciones
# --------------------------------------------------------------------------------------

def validar_csv_vs_pdf(tco_ops: dict, serie_pdf: dict):
    """Compara el TCO del CSV de operaciones contra el PDF anual, por fecha de vigencia."""
    comunes = sorted(set(tco_ops) & set(serie_pdf))
    solo_csv = sorted(set(tco_ops) - set(serie_pdf))
    solo_pdf = sorted(v for v in serie_pdf
                      if serie_pdf[v]["_oficial"] is not None and v not in tco_ops
                      and dt.date.fromisoformat(v) >= PRIMER_CORTE)

    coincidencias, diferencias = 0, []
    for v in comunes:
        val_csv = tco_ops[v][2]
        val_pdf = serie_pdf[v]["_oficial"]
        if val_pdf is None:
            diferencias.append((v, val_csv, None, None))
            continue
        if abs(val_csv - val_pdf) <= TOL_TCO:
            coincidencias += 1
        else:
            diferencias.append((v, val_csv, val_pdf, val_csv - val_pdf))

    # Sabados y domingos nunca tienen reporte de operaciones: el TCO del viernes sigue
    # vigente. No es una falta de dato, asi que se cuenta aparte.
    finde = [v for v in solo_pdf if dt.date.fromisoformat(v).weekday() >= 5]
    habiles = [v for v in solo_pdf if v not in finde]

    pct = (coincidencias / len(comunes) * 100) if comunes else 0.0
    print()
    print("--- Validacion CSV vs PDF (TCO oficial por fecha de vigencia) ---")
    print(f"    fechas comparadas  : {len(comunes)}")
    print(f"    coincidencias      : {coincidencias}")
    print(f"    diferencias        : {len(diferencias)}")
    print(f"    porcentaje         : {pct:.2f}%")
    print(f"    faltantes en CSV   : {len(solo_pdf)} "
          f"({len(finde)} fines de semana, {len(habiles)} dias habiles)")
    if habiles:
        print(f"        dias habiles sin reporte de operaciones: {habiles}")
    print(f"    faltantes en PDF   : {len(solo_csv)}" + (f" -> {solo_csv}" if solo_csv else ""))
    if diferencias:
        print("    fecha        CSV        PDF        diferencia")
        for v, c, p, d in diferencias:
            error(f"{v} CSV={c} PDF={p} dif={d}")
    else:
        ok(f"CSV vs PDF: {pct:.2f}% coincidencia ({coincidencias}/{len(comunes)})")

    for v in solo_csv:
        aviso(f"Vigencia {v} esta en el CSV de operaciones y no en el PDF anual.")
    for v in habiles:
        aviso(f"Vigencia {v} (dia habil) publicada en el PDF y aun sin reporte de operaciones.")

    return {"comparadas": len(comunes), "coincidencias": coincidencias,
            "diferencias": len(diferencias), "porcentaje": pct,
            "faltan_csv": len(solo_pdf), "faltan_pdf": len(solo_csv)}


def validar_operaciones(detalle, bancos, tot_tc):
    """Agregados por banco, suma contra TOTAL BANCOS y TCO ponderado."""
    agr = {}
    for d in detalle:
        a = agr.setdefault((d["fecha_corte"], d["banco"]), {"n": 0.0, "m": 0.0, "tcm": 0.0, "k": 0})
        a["n"] += d["_n"] or 0
        a["m"] += d["_m"] or 0
        a["tcm"] += d["_tc"] * (d["_m"] or 0)
        a["k"] += 1

    pub = {(b["fecha_corte"], b["banco"]): b for b in bancos}
    dif_n = dif_m = dif_pond = dif_tc = sin_dato = comparadas = 0

    for (corte, banco), a in sorted(agr.items()):
        p = pub.get((corte, banco))
        if p is None:
            error(f"{corte} {banco}: hay detalle pero no fila TOTAL en la fuente.")
            continue
        comparadas += 1
        if p["_n"] is None or p["_m"] is None:
            sin_dato += 1
            aviso(f"{corte} {banco}: la fuente no publica el TOTAL (N° o Monto); no se valida.")
        else:
            if abs(a["n"] - p["_n"]) > 1e-9:
                dif_n += 1
                error(f"{corte} {banco}: operaciones detalle={a['n']:.0f} vs agregado={p['_n']:.0f}")
            tol = TOL_REDONDEO_CELDA * a["k"] + 1e-6
            if abs(a["m"] - p["_m"]) > tol:
                dif_m += 1
                error(f"{corte} {banco}: monto detalle={a['m']:.0f} vs agregado={p['_m']:.0f} "
                      f"(tolerancia de redondeo {tol:.1f})")
        if p["_tco"] is not None and a["m"] > 0:
            pond = a["tcm"] / a["m"]
            if abs(pond - p["_tco"]) > TOL_PONDERADO:
                dif_pond += 1
                error(f"{corte} {banco}: TCO ponderado={pond:.6f} vs publicado={p['_tco']:.2f} "
                      f"dif={pond - p['_tco']:+.6f}")

    suma_tc = {}
    for d in detalle:
        s = suma_tc.setdefault((d["fecha_corte"], d["tc"]), [0.0, 0.0, 0])
        s[0] += d["_n"] or 0
        s[1] += d["_m"] or 0
        s[2] += 1
    for k, (n, m, k_celdas) in sorted(suma_tc.items()):
        t = tot_tc.get(k)
        if t is None:
            continue
        if t[0] is not None and abs(n - t[0]) > 1e-9:
            dif_tc += 1
            error(f"{k[0]} TC={k[1]}: suma bancos N°={n:.0f} vs TOTAL BANCOS={t[0]:.0f}")
        if t[1] is not None and abs(m - t[1]) > TOL_REDONDEO_CELDA * k_celdas + 1e-6:
            dif_tc += 1
            error(f"{k[0]} TC={k[1]}: suma bancos monto={m:.0f} vs TOTAL BANCOS={t[1]:.0f}")

    print()
    print("--- Validacion interna de operaciones ---")
    print(f"    pares fecha x banco comparados : {comparadas}")
    print(f"    diferencias en N° operaciones  : {dif_n}")
    print(f"    diferencias en monto USD       : {dif_m}   (tolerancia {TOL_REDONDEO_CELDA}/celda)")
    print(f"    diferencias en TCO ponderado   : {dif_pond} (tolerancia {TOL_PONDERADO})")
    print(f"    filas TC vs TOTAL BANCOS       : {dif_tc}")
    print(f"    agregados no publicados        : {sin_dato}")
    if dif_n == dif_m == dif_pond == dif_tc == 0:
        ok(f"detalle.csv y bancos.csv validados ({comparadas} pares fecha x banco)")

    return {"comparadas": comparadas, "dif_n": dif_n, "dif_m": dif_m,
            "dif_pond": dif_pond, "dif_tc": dif_tc, "sin_dato": sin_dato}


def validar_estructura(tco, bancos, detalle) -> bool:
    """Claves de deduplicacion explicitas y orden cronologico."""
    problemas = 0
    for nombre, filas, clave, orden in (
        ("tco.csv", tco, lambda f: (f["vigencia"],), lambda f: f["vigencia"]),
        ("bancos.csv", bancos, lambda f: (f["fecha_corte"], f["banco"]), lambda f: f["fecha_corte"]),
        ("detalle.csv", detalle, lambda f: (f["fecha_corte"], f["banco"], f["tc"]),
         lambda f: f["fecha_corte"]),
    ):
        claves = [clave(f) for f in filas]
        if len(claves) != len(set(claves)):
            vistos, dup = set(), []
            for k in claves:
                if k in vistos and k not in dup:
                    dup.append(k)
                vistos.add(k)
            error(f"{nombre}: claves duplicadas, ej. {dup[:5]}")
            problemas += 1
        fechas = [orden(f) for f in filas]
        if fechas != sorted(fechas):
            error(f"{nombre}: filas fuera de orden cronologico.")
            problemas += 1
    if problemas == 0:
        ok("estructura: sin duplicados y en orden cronologico")
    return problemas == 0


# --------------------------------------------------------------------------------------
# raw.zip  (deduplicado por SHA-256, bytes deterministas)
# --------------------------------------------------------------------------------------

def actualizar_raw(nuevos: dict) -> tuple[bool, int]:
    """nuevos: {nombre_en_zip: (bytes, huella_de_contenido | None)}.

    Conserva lo ya archivado y solo reemplaza cuando el contenido cambia. Para los
    archivos con huella propia (el PDF anual) la comparacion usa esa huella, porque el
    BCB regenera el PDF en cada descarga con metadatos nuevos y sus bytes nunca se
    repiten. Para el resto se compara el SHA-256 del archivo.
    """
    miembros: dict[str, bytes] = {}
    huellas: dict[str, str] = {}
    if os.path.exists(RAW_ZIP):
        try:
            with zipfile.ZipFile(RAW_ZIP) as z:
                nombres = z.namelist()
                for n in nombres:
                    if n != "manifest.json":
                        miembros[n] = z.read(n)
                if "manifest.json" in nombres:
                    previo = json.loads(z.read("manifest.json").decode("utf-8"))
                    huellas = {n: d["huella"] for n, d in previo.items() if d.get("huella")}
        except (zipfile.BadZipFile, ValueError, KeyError):
            error("data/raw.zip existente esta corrupto; se regenera desde cero.")
            miembros, huellas = {}, {}

    for nombre, (contenido, huella) in nuevos.items():
        if huella is not None:
            distinto = huellas.get(nombre) != huella
        else:
            anterior = miembros.get(nombre)
            distinto = anterior is None or \
                hashlib.sha256(anterior).hexdigest() != hashlib.sha256(contenido).hexdigest()
        if distinto:
            miembros[nombre] = contenido
            if huella is not None:
                huellas[nombre] = huella

    manifiesto = json.dumps(
        {n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b),
             **({"huella": huellas[n]} if n in huellas else {})}
         for n, b in sorted(miembros.items())},
        indent=1, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for nombre in sorted(list(miembros) + ["manifest.json"]):
            datos = manifiesto if nombre == "manifest.json" else miembros[nombre]
            info = zipfile.ZipInfo(nombre, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, datos)
    nuevo = buf.getvalue()

    anterior = None
    if os.path.exists(RAW_ZIP):
        with open(RAW_ZIP, "rb") as fh:
            anterior = fh.read()
    if anterior == nuevo:
        return False, len(miembros)
    with open(RAW_ZIP, "wb") as fh:
        fh.write(nuevo)
    return True, len(miembros)


# --------------------------------------------------------------------------------------
# Certificado de estado (status/status.png)
# --------------------------------------------------------------------------------------

def _serif() -> str:
    from matplotlib import font_manager
    disponibles = {f.name for f in font_manager.fontManager.ttflist}
    for nombre in ("Latin Modern Roman", "CMU Serif", "Nimbus Roman", "Liberation Serif",
                   "DejaVu Serif", "Times New Roman"):
        if nombre in disponibles:
            return nombre
    return "serif"


def generar_status(payload: dict) -> bool:
    """Dibuja el certificado. Solo reescribe si cambia algo distinto de la hora de corrida."""
    huella = json.dumps({k: v for k, v in payload.items() if k != "corrida"},
                        sort_keys=True, ensure_ascii=False)
    marca = os.path.join(STATUS, ".huella")
    previa = None
    if os.path.exists(marca):
        with open(marca, "r", encoding="utf-8") as fh:
            previa = fh.read()
    if previa == huella and os.path.exists(PNG_STATUS) and not os.environ.get("TCO_SELLAR"):
        ok("status.png sin cambios (mismo estado que la corrida anterior)")
        return False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    CREMA, VERDE, ORO, TINTA, ROJO = "#F7F2E4", "#14452F", "#B08D4F", "#1E1B16", "#8E2B20"
    exito = payload["resultado"] == "EXITOSO"
    acento = VERDE if exito else ROJO
    serif = _serif()

    fig = plt.figure(figsize=(8.0, 11.0), dpi=150)
    fig.patch.set_facecolor(CREMA)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    ax.set_facecolor(CREMA)

    ax.add_patch(Rectangle((4, 3), 92, 94, fill=False, ec=acento, lw=2.2))
    ax.add_patch(Rectangle((5.4, 4.2), 89.2, 91.6, fill=False, ec=ORO, lw=0.8))

    T = dict(ha="center", family=serif)
    ax.text(50, 90.5, "T C O — B C B", size=27, color=acento, **T)
    ax.text(50, 87.4, "Certificado de estado de la ultima corrida", size=11.5,
            color=TINTA, style="italic", **T)
    ax.plot([26, 74], [85.6, 85.6], color=ORO, lw=1.0)

    ax.text(50, 82.2, "RESULTADO GENERAL", size=9, color=TINTA, **T)
    ax.add_patch(Rectangle((31, 75.0), 38, 5.9, fill=False, ec=acento, lw=1.4))
    ax.text(50, 76.5, payload["resultado"], size=21, color=acento, **T)

    def bloque(y0, titulo, pares):
        ax.text(11, y0, titulo.upper(), size=8.5, color=ORO, family=serif, ha="left")
        ax.plot([11, 89], [y0 - 1.2, y0 - 1.2], color=ORO, lw=0.6, alpha=0.65)
        y = y0 - 3.6
        for etiqueta, valor, color in pares:
            ax.text(13, y, etiqueta, size=10, color=TINTA, family=serif, ha="left")
            ax.text(87, y, str(valor), size=10, color=color or TINTA, family=serif,
                    ha="right", weight="bold" if color else "normal")
            y -= 3.3
        return y

    def col(v):
        return VERDE if v in ("OK", "SIN CAMBIOS") else ROJO

    y = bloque(70.0, "Informacion de la corrida", [
        ("Ultima corrida", payload["corrida"], None),
        ("Ultima fecha BCB disponible", payload["ultima_fecha"], None),
        ("Ultimo TCO", payload["ultimo_tco"], None),
        ("Ultimo corte de operaciones", payload["ultimo_corte"], None),
        ("Actualizacion programada", "22:00 (Bolivia)", None),
    ])
    y = bloque(y - 1.8, "Archivos", [
        ("data/tco.csv", payload["tco"], col(payload["tco"])),
        ("data/bancos.csv", payload["bancos"], col(payload["bancos"])),
        ("data/detalle.csv", payload["detalle"], col(payload["detalle"])),
        ("data/raw.zip", payload["raw"], col(payload["raw"])),
    ])
    y = bloque(y - 1.8, "Validaciones", [
        ("Validacion CSV vs PDF", payload["val_pdf"], col(payload["val_pdf"])),
        ("Validacion de operaciones", payload["val_ops"], col(payload["val_ops"])),
        ("Estructura y duplicados", payload["val_est"], col(payload["val_est"])),
    ])

    ax.text(50, y - 1.0, payload["detalle_texto"], size=8.5, color=TINTA, style="italic", **T)

    ax.plot([26, 74], [9.4, 9.4], color=ORO, lw=0.8)
    ax.text(50, 7.2, "Documento generado automaticamente por TCO-BCB", size=8.5, color=TINTA, **T)
    ax.text(50, 5.2, "Proyecto independiente basado en fuentes publicas del Banco Central de Bolivia",
            size=7, color="#6B6459", **T)

    fig.savefig(PNG_STATUS, facecolor=CREMA)
    plt.close(fig)
    with open(marca, "w", encoding="utf-8") as fh:
        fh.write(huella)
    ok("status.png generado")
    return True


# --------------------------------------------------------------------------------------
# Flujo principal
# --------------------------------------------------------------------------------------

def ejecutar(reconstruir: bool = False) -> int:
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(STATUS, exist_ok=True)
    ahora = dt.datetime.now(HUSO_BOLIVIA)

    # Paso 1: estado local ------------------------------------------------------------
    print("=" * 78)
    print(f"TCO-BCB  |  corrida {ahora:%Y-%m-%d %H:%M} (Bolivia)"
          + ("  |  RECONSTRUCCION COMPLETA" if reconstruir else ""))
    print("=" * 78)
    if reconstruir:
        for ruta in (CSV_TCO, CSV_BANCOS, CSV_DETALLE):
            if os.path.exists(ruta):
                os.remove(ruta)
    prev_vig = leer_ultima_fecha(CSV_TCO, "vigencia")
    prev_corte = leer_ultima_fecha(CSV_BANCOS, "fecha_corte")
    print(f"[..] Estado local: ultima vigencia={prev_vig or 'sin datos'} | "
          f"ultimo corte={prev_corte or 'sin datos'}")

    # Paso 2: fuentes oficiales -------------------------------------------------------
    try:
        descargar(URL_SERIE_TCO)
        ok("Fuente BCB accesible (pagina oficial de la serie de TCO)")
    except Exception as exc:  # noqa: BLE001
        error(f"No se pudo acceder a la pagina oficial de la serie: {exc}")

    crudos: dict[str, tuple[bytes, str | None]] = {}
    serie_pdf: dict[str, dict] = {}
    for anio in sorted({FECHA_INICIO.year, ahora.year}):
        pdf = descargar(URL_HIST_PDF.format(anio=anio))
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError(f"La respuesta de pdf.php?anio={anio} no es un PDF.")
        serie, huella = parsear_pdf_anual(pdf, anio)
        serie_pdf.update(serie)
        crudos[f"historico/{anio}.pdf"] = (pdf, huella)
        crudos[f"tco/{anio}.html"] = (descargar(URL_HIST_HTML.format(anio=anio)), None)
    ok(f"PDF anual leido: {len(serie_pdf)} dias con cotizacion")

    hasta = (ahora.date() + dt.timedelta(days=3)).isoformat()
    ops_bytes = descargar(URL_OPERACIONES.format(desde=PRIMER_CORTE.isoformat(), hasta=hasta))
    detalle, bancos, tco_ops, cortes, tot_tc = parsear_operaciones(ops_bytes)
    ok(f"CSV de operaciones leido: {len(cortes)} fechas de corte, {len(detalle)} filas de detalle")

    # Paso 3: novedad segun fechas realmente publicadas -------------------------------
    # El endpoint nunca responde vacio: si el rango pedido no tiene datos devuelve el
    # ultimo corte disponible. Por eso la novedad se decide comparando fechas publicadas
    # contra las almacenadas, nunca asumiendo hoy + 1 dia.
    ult_corte = cortes[-1] if cortes else None
    ult_vig = max((v for v in serie_pdf
                   if serie_pdf[v]["tco_oficial"] or serie_pdf[v]["tco_venta"]), default=None)
    ult_tco = ""
    if ult_vig:
        r = serie_pdf[ult_vig]
        ult_tco = r["tco_oficial"] or r["tco_venta"]
    print(f"[..] Estado BCB  : ultima vigencia={ult_vig} | ultimo corte={ult_corte}")

    # Paso 5: actualizar las bases ----------------------------------------------------
    filas_tco = construir_tco(serie_pdf, tco_ops)
    detalle.sort(key=lambda d: (d["fecha_corte"], d["banco"], d["_tc"]))
    bancos.sort(key=lambda b: (b["fecha_corte"], b["banco"] == TOTAL_BANCOS, b["banco"]))

    est_ok = validar_estructura(filas_tco, bancos, detalle)

    cambio_tco = escribir_csv(CSV_TCO,
                              ["fecha_corte", "vigencia", "tco_compra", "tco_venta", "tco_oficial"],
                              filas_tco)
    cambio_ban = escribir_csv(CSV_BANCOS,
                              ["fecha_corte", "vigencia", "banco", "operaciones", "monto_usd", "tco"],
                              bancos)
    cambio_det = escribir_csv(CSV_DETALLE,
                              ["fecha_corte", "vigencia", "banco", "tc", "operaciones", "monto_usd"],
                              detalle)
    for nombre, cambio, n in (("tco.csv", cambio_tco, len(filas_tco)),
                              ("bancos.csv", cambio_ban, len(bancos)),
                              ("detalle.csv", cambio_det, len(detalle))):
        ok(f"{nombre} {'actualizado' if cambio else 'sin cambios'} ({n} filas)")

    # Paso 4 / 6: sin novedad y validaciones -------------------------------------------
    hay_novedad = cambio_tco or cambio_ban or cambio_det
    if not hay_novedad:
        print("NO_NEW_DATA")

    res_pdf = validar_csv_vs_pdf(tco_ops, serie_pdf)
    res_ops = validar_operaciones(detalle, bancos, tot_tc)

    # Paso 7: archivos originales ------------------------------------------------------
    lineas = ops_bytes.decode("utf-8-sig", "replace").splitlines()
    i_cab = next((i for i, l in enumerate(lineas)
                  if l.lower().startswith('"fecha de corte"')), None)
    encabezado = lineas[i_cab:i_cab + 2] if i_cab is not None else []
    por_corte: dict[str, list[str]] = {c: [] for c in cortes}
    for l in lineas:
        c = l.split(";", 1)[0]
        if c in por_corte:
            por_corte[c].append(l)
    for corte, filas in por_corte.items():
        cuerpo = ("\n".join(encabezado + filas) + "\n").encode("utf-8")
        crudos[f"operaciones/{corte}.csv"] = (cuerpo, None)

    cambio_raw, n_raw = actualizar_raw(crudos)
    ok(f"raw.zip {'actualizado' if cambio_raw else 'sin cambios'} ({n_raw} archivos originales)")

    # Paso 8: certificado --------------------------------------------------------------
    val_pdf_ok = res_pdf["diferencias"] == 0
    val_ops_ok = res_ops["dif_n"] == res_ops["dif_m"] == res_ops["dif_pond"] == res_ops["dif_tc"] == 0
    resultado = "EXITOSO" if not ERRORES else "ERROR"

    generar_status({
        "resultado": resultado,
        "corrida": f"{ahora:%Y-%m-%d %H:%M} (Bolivia)",
        "ultima_fecha": ult_vig or "sin datos",
        "ultimo_tco": f"{ult_tco} Bs/USD" if ult_tco else "sin datos",
        "ultimo_corte": ult_corte or "sin datos",
        "tco": "OK" if cambio_tco else "SIN CAMBIOS",
        "bancos": "OK" if cambio_ban else "SIN CAMBIOS",
        "detalle": "OK" if cambio_det else "SIN CAMBIOS",
        "raw": "OK" if cambio_raw else "SIN CAMBIOS",
        "val_pdf": "OK" if val_pdf_ok else "DIFERENCIAS",
        "val_ops": "OK" if val_ops_ok else "DIFERENCIAS",
        "val_est": "OK" if est_ok else "DIFERENCIAS",
        "detalle_texto": (f"CSV vs PDF: {res_pdf['coincidencias']}/{res_pdf['comparadas']} fechas "
                          f"({res_pdf['porcentaje']:.2f}%)   |   "
                          f"{res_ops['comparadas']} pares fecha x banco validados"),
    })

    print()
    print("-" * 78)
    print(f"RESULTADO: {resultado} | errores={len(ERRORES)} avisos={len(AVISOS)} | "
          f"cambios={'si' if hay_novedad or cambio_raw else 'no'}")
    print("-" * 78)
    return 1 if ERRORES else 0


def main() -> int:
    try:
        return ejecutar(reconstruir="--reconstruir" in sys.argv)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        error(f"Fallo no controlado: {type(exc).__name__}: {exc}")
        try:
            generar_status({
                "resultado": "ERROR",
                "corrida": f"{dt.datetime.now(HUSO_BOLIVIA):%Y-%m-%d %H:%M} (Bolivia)",
                "ultima_fecha": leer_ultima_fecha(CSV_TCO, "vigencia") or "sin datos",
                "ultimo_tco": "sin datos", "ultimo_corte": "sin datos",
                "tco": "ERROR", "bancos": "ERROR", "detalle": "ERROR", "raw": "ERROR",
                "val_pdf": "ERROR", "val_ops": "ERROR", "val_est": "ERROR",
                "detalle_texto": f"{type(exc).__name__}: {str(exc)[:90]}",
            })
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
