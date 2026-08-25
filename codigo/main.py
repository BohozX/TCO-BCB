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
from decimal import Decimal

import requests

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATOS = os.path.join(RAIZ, "datos")
ESTADO = os.path.join(RAIZ, "estado")

CSV_TCO = os.path.join(DATOS, "tco.csv")
CSV_BANCOS = os.path.join(DATOS, "bancos.csv")
CSV_DETALLE = os.path.join(DATOS, "detalle.csv")
RAW_ZIP = os.path.join(DATOS, "raw.zip")
PNG_ESTADO = os.path.join(ESTADO, "status.png")

BASE = "https://www.bcb.gob.bo/"
URL_SERIE = BASE + "?q=content/tipo-de-cambio-oficial-del-d%C3%B3lar-estadounidense-serie-de-tiempo"
URL_HTML = BASE + "tiposDeCambioHistorico/index.php?anio={anio}"
URL_PDF = BASE + "tiposDeCambioHistorico/pdf.php?anio={anio}"
# El BCB publica el detalle de operaciones por dos endpoints distintos. Se leen los
# dos: el publico es el que alimenta la pagina oficial y trae los cortes recientes;
# el legado quedo congelado en el corte 2026-08-20 pero se conserva porque cubre el
# mismo historico y permite contrastar una fuente contra la otra.
URL_OPERACIONES = [
    ("publico", BASE + "bcb_tco_publico_descargar_csv.php?desde={desde}&hasta={hasta}"),
    ("legado", BASE + "tco_tcreferencial_descargar_csv.php?desde={desde}&hasta={hasta}"),
]

FECHA_INICIO = dt.date(2026, 6, 1)
PRIMER_CORTE = dt.date(2026, 6, 26)

SPREAD = Decimal("0.10")
TOTAL_BANCOS = "TOTAL BANCOS"
HUSO_BOLIVIA = dt.timezone(dt.timedelta(hours=-4))

TOL_TCO = 1e-4
TOL_REDONDEO_CELDA = 0.5
TOL_PONDERADO = 0.005
# Los dos endpoints redondean los agregados por separado, asi que un mismo total
# puede diferir en algun dolar entre uno y otro.
TOL_FUENTES = 2.0

MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
    "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}
CLASES = {"VENTA": "venta", "COMPRA": "compra", "OFICIAL": "oficial"}

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


def literal(txt: str) -> str:
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


ACENTOS = str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNaeiouun")


def clave_banco(nombre: str) -> str:
    """Los dos endpoints escriben los bancos distinto (BANCO DE CREDITO vs
    BANCO DE CRÉDITO). Se comparan sin tildes; el nombre que se publica conserva
    la forma acentuada cuando alguna de las fuentes la trae."""
    return " ".join(nombre.translate(ACENTOS).upper().split())


def dias_de_vigencia(txt: str) -> list[str]:
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


def parsear_operaciones(contenido: bytes):
    filas = list(csv.reader(contenido.decode("utf-8-sig", "replace").splitlines(), delimiter=";"))

    idx = None
    for i, f in enumerate(filas):
        if f and f[0].strip().lower().startswith("fecha de corte"):
            idx = i
            break
    if idx is None:
        raise RuntimeError("El CSV de operaciones no contiene la fila de encabezado esperada.")

    cab = filas[idx]
    columnas = [(cab[j].strip(), j) for j in range(3, len(cab), 2) if cab[j].strip()]
    if not columnas or columnas[-1][0] != TOTAL_BANCOS:
        raise RuntimeError(f"Encabezado de bancos inesperado: {[c[0] for c in columnas]}")
    orden_bancos = [b for b, _ in columnas]

    datos = [f for f in filas[idx + 2:]
             if len(f) > 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[0].strip())]

    detalle, bancos, tco_ops, cortes, vistos = [], [], {}, [], set()

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
                    "Monto": literal(cm),
                    "_n": numero(cn), "_m": numero(cm), "_tco": None,
                })
            elif etiqueta == "TCO":
                if banco == TOTAL_BANCOS and numero(cn) is not None:
                    for dia in dias_de_vigencia(vigencia):
                        tco_ops[dia] = (corte, numero(cn))
                for b in reversed(bancos):
                    if b["fecha_corte"] == corte and b["banco"] == banco:
                        b["_tco"] = numero(cn)
                        break
            else:
                tc = literal(etiqueta)
                if not tc or numero(etiqueta) is None:
                    continue
                if numero(cn) is None and numero(cm) is None:
                    continue
                detalle.append({
                    "fecha_corte": corte, "vigencia": vigencia, "banco": banco, "tc": tc,
                    "Monto": literal(cm),
                    "_tc": numero(etiqueta), "_n": numero(cn), "_m": numero(cm),
                })

    return detalle, bancos, tco_ops, cortes, orden_bancos


CHOQUES = [0]


def fundir(base, otra, donde):
    """Completa en 'base' lo que solo trae 'otra', y contrasta lo que ambas
    publican. Un hueco en una fuente no es un conflicto: es justo lo que la otra
    viene a cubrir. Solo se reporta cuando las dos dan un valor y no cuadran."""
    completados = 0
    for texto, num in (("Monto", "_m"),):
        if not base[texto] and otra[texto]:
            base[texto], base[num] = otra[texto], otra[num]
            completados += 1
        elif base[num] is not None and otra[num] is not None \
                and abs(base[num] - otra[num]) > TOL_FUENTES:
            CHOQUES[0] += 1
            error(f"{donde}: monto distinto entre fuentes "
                  f"({base[num]:.0f} vs {otra[num]:.0f})")

    for campo in ("_n", "_tco"):
        if campo not in base:
            continue
        if base[campo] is None and otra.get(campo) is not None:
            base[campo] = otra[campo]
            completados += 1
        elif base[campo] is not None and otra.get(campo) is not None:
            tol = TOL_TCO if campo == "_tco" else 0.5
            if abs(base[campo] - otra[campo]) > tol:
                CHOQUES[0] += 1
                error(f"{donde}: {campo} distinto entre fuentes "
                      f"({base[campo]} vs {otra[campo]})")
    return completados


def combinar_operaciones(fuentes):
    """Une lo que publica cada endpoint. La primera fuente manda, las siguientes
    rellenan lo que a la primera le falte, y donde ambas coinciden se contrastan.
    Asi, si una se queda congelada, la otra sostiene la serie."""
    nombres, detalle, bancos, tco_ops, orden = {}, {}, {}, {}, []
    aporte, completados = {}, 0
    CHOQUES[0] = 0
    tildes = lambda t: sum(1 for c in t if ord(c) > 127)

    for etiqueta, (det, ban, tco, cortes_f, orden_f) in fuentes:
        aporte[etiqueta] = 0

        for banco in orden_f:
            k = clave_banco(banco)
            if k not in nombres or tildes(banco) > tildes(nombres[k]):
                nombres[k] = banco
            if k not in orden:
                orden.append(k)

        for fila in det:
            k = (fila["fecha_corte"], clave_banco(fila["banco"]), fila["tc"])
            if k not in detalle:
                detalle[k] = fila
                aporte[etiqueta] += 1
                continue
            completados += fundir(detalle[k], fila, f"{fila['fecha_corte']} {fila['banco']} "
                                                    f"TC={fila['tc']}")

        for fila in ban:
            k = (fila["fecha_corte"], clave_banco(fila["banco"]))
            if k not in bancos:
                bancos[k] = fila
                continue
            completados += fundir(bancos[k], fila, f"{fila['fecha_corte']} {fila['banco']}")

        for dia, valor in tco.items():
            if dia in tco_ops:
                if abs(tco_ops[dia][1] - valor[1]) > TOL_TCO:
                    CHOQUES[0] += 1
                    error(f"Vigencia {dia}: TCO distinto entre fuentes "
                          f"({tco_ops[dia][1]} vs {valor[1]})")
                continue
            tco_ops[dia] = valor

    for fila in list(detalle.values()) + list(bancos.values()):
        fila["banco"] = nombres[clave_banco(fila["banco"])]

    cortes = sorted({f["fecha_corte"] for f in bancos.values()})
    clave_total = clave_banco(TOTAL_BANCOS)
    orden_bancos = [nombres[k] for k in orden if k != clave_total]
    orden_bancos.append(nombres.get(clave_total, TOTAL_BANCOS))

    print()
    print("--- Fuentes de operaciones ---")
    for etiqueta, (_, _, _, cs, _) in fuentes:
        rango = f"{cs[0]} -> {cs[-1]}" if cs else "sin datos"
        print(f"    {etiqueta:9s}: {len(cs):3d} cortes ({rango})")
    print(f"    combinado : {len(cortes):3d} cortes | filas nuevas por fuente: "
          + ", ".join(f"{k}={v}" for k, v in aporte.items()))
    print(f"    huecos de una fuente cubiertos por la otra: {completados}")
    if CHOQUES[0] == 0 and len(fuentes) > 1:
        ok(f"las fuentes coinciden en todo lo que ambas publican "
           f"(tolerancia {TOL_FUENTES:.0f} USD por redondeo)")

    return list(detalle.values()), list(bancos.values()), tco_ops, cortes, orden_bancos


def parsear_pdf_anual(contenido: bytes, anio: int):
    import pdfplumber

    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        palabras = pdf.pages[0].extract_words()
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
                       if any(w["text"].upper() in CLASES for w in ln)), None)
    if fila_tipos is None:
        raise RuntimeError(f"El PDF {anio} no contiene la fila VENTA/COMPRA/OFICIAL.")

    cab_meses = [w["text"].upper() for w in fila_meses[1:] if w["text"].upper() in MESES]
    meses = [(MESES[m], centro(w)) for m, w in
             zip(cab_meses, [w for w in fila_meses[1:] if w["text"].upper() in MESES])]
    if not meses:
        raise RuntimeError(f"No se reconocio ningun mes en el encabezado del PDF {anio}.")
    solapados = {MESES[m] for m in cab_meses if cab_meses.count(m) > 1}

    columnas = []
    for w in fila_tipos:
        clase = w["text"].upper()
        if clase in CLASES:
            cx = centro(w)
            columnas.append((cx, min(meses, key=lambda m: abs(m[1] - cx))[0], clase))
    if not columnas:
        raise RuntimeError(f"No se reconocio ninguna subcolumna en el PDF {anio}.")

    serie: dict[str, dict] = {}
    for ln in lineas[i_meses + 2:]:
        if not ln or not ln[0]["text"].strip().isdigit():
            continue
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
                continue
            reg = serie.setdefault(fecha, {"venta": "", "compra": "", "oficial": ""})
            campo = CLASES[clase]
            if reg[campo] and abs(float(reg[campo]) - val) > TOL_TCO:
                error(f"PDF {anio}: valor inconsistente en {fecha} {campo}: {reg[campo]} vs {val}")
            reg[campo] = literal(w["text"])

    return serie, huella, solapados


def parsear_html_anual(contenido: bytes, anio: int):
    from lxml import html as LH

    doc = LH.fromstring(contenido.decode("utf-8", "replace"))
    tablas = doc.xpath("//table")
    if not tablas:
        raise RuntimeError(f"El HTML {anio} no contiene ninguna tabla.")

    meses, serie = None, {}
    for r in tablas[0].xpath(".//tr"):
        celdas = [re.sub(r"\s+", " ", (c.text_content() or "")).strip()
                  for c in r.xpath("./th|./td")]
        if not celdas:
            continue
        if celdas[0].upper() == "DIAS":
            meses = [MESES[m.upper()] for m in celdas[1:] if m.upper() in MESES]
            continue
        if meses is None or not celdas[0].isdigit():
            continue
        dia, vals = int(celdas[0]), celdas[1:]
        for i, mes in enumerate(meses):
            v = literal(vals[2 * i]) if 2 * i < len(vals) else ""
            c = literal(vals[2 * i + 1]) if 2 * i + 1 < len(vals) else ""
            if not v and not c:
                continue
            try:
                serie[dt.date(anio, mes, dia).isoformat()] = {"venta": v, "compra": c}
            except ValueError:
                continue
    return serie


def construir_tco(serie_pdf: dict, tco_ops: dict):
    filas = []
    for vigencia in sorted(serie_pdf):
        if dt.date.fromisoformat(vigencia) < FECHA_INICIO:
            continue
        reg = serie_pdf[vigencia]
        compra = reg["compra"] or reg["oficial"]
        if not compra:
            continue
        venta = reg["venta"] or str(Decimal(compra) + SPREAD)
        filas.append({
            "fecha_corte": tco_ops.get(vigencia, ("", None))[0],
            "vigencia": vigencia,
            "tco_compra": compra,
            "tco_venta": venta,
        })
    return filas


def empaquetar_detalle(detalle, bancos):
    por, vigencias = {}, {}
    for d in detalle:
        if d["banco"] not in bancos:
            continue
        vigencias[d["fecha_corte"]] = d["vigencia"]
        por.setdefault((d["fecha_corte"], d["banco"]), []).append((d["_tc"], d["tc"], d["Monto"]))

    filas = []
    for fecha in sorted(vigencias):
        listas = {b: sorted(por.get((fecha, b), [])) for b in bancos}
        for i in range(max((len(v) for v in listas.values()), default=0)):
            fila = {"fecha_corte": fecha, "vigencia": vigencias[fecha]}
            for b in bancos:
                v = listas[b]
                if i < len(v):
                    fila[f"{b} Precio"] = v[i][1]
                    fila[f"{b} Monto"] = v[i][2]
            filas.append(fila)

    campos = ["fecha_corte", "vigencia"] + [f"{b} {s}" for b in bancos for s in ("Precio", "Monto")]
    return campos, filas


def agregar_bancos(bancos_filas, orden):
    por = {}
    for b in bancos_filas:
        fila = por.setdefault(b["fecha_corte"],
                              {"fecha_corte": b["fecha_corte"], "vigencia": b["vigencia"]})
        fila[f"{b['banco']} Monto"] = b["Monto"]
    campos = ["fecha_corte", "vigencia"] + [f"{b} Monto" for b in orden]
    return campos, [por[k] for k in sorted(por)]


def escribir_csv(ruta: str, campos: list[str], filas: list[dict]) -> bool:
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


def validar_csv_vs_pdf(tco_ops: dict, serie_pdf: dict):
    def oficial(v):
        return float(serie_pdf[v]["oficial"]) if serie_pdf[v]["oficial"] else None

    comunes = sorted(set(tco_ops) & set(serie_pdf))
    solo_csv = sorted(set(tco_ops) - set(serie_pdf))
    solo_pdf = sorted(v for v in serie_pdf if oficial(v) is not None and v not in tco_ops
                      and dt.date.fromisoformat(v) >= PRIMER_CORTE)

    coincidencias, diferencias = 0, []
    for v in comunes:
        a, b = tco_ops[v][1], oficial(v)
        if b is None:
            diferencias.append((v, a, None, None))
        elif abs(a - b) <= TOL_TCO:
            coincidencias += 1
        else:
            diferencias.append((v, a, b, a - b))

    finde = [v for v in solo_pdf if dt.date.fromisoformat(v).weekday() >= 5]
    habiles = [v for v in solo_pdf if v not in finde]
    pct = (coincidencias / len(comunes) * 100) if comunes else 0.0

    print()
    print("--- Validacion CSV vs PDF ---")
    print(f"    fechas comparadas  : {len(comunes)}")
    print(f"    coincidencias      : {coincidencias}")
    print(f"    diferencias        : {len(diferencias)}")
    print(f"    porcentaje         : {pct:.2f}%")
    print(f"    faltantes en CSV   : {len(solo_pdf)} "
          f"({len(finde)} fines de semana, {len(habiles)} dias habiles)")
    if habiles:
        print(f"        dias habiles sin operaciones: {habiles}")
    print(f"    faltantes en PDF   : {len(solo_csv)}" + (f" -> {solo_csv}" if solo_csv else ""))

    if diferencias:
        print("    fecha        CSV        PDF        diferencia")
        for v, a, b, d in diferencias:
            error(f"{v} CSV={a} PDF={b} dif={d}")
    else:
        ok(f"CSV vs PDF: {pct:.2f}% coincidencia ({coincidencias}/{len(comunes)})")

    for v in solo_csv:
        aviso(f"Vigencia {v} esta en el CSV de operaciones y no en el PDF anual.")
    for v in habiles:
        aviso(f"Vigencia {v} (dia habil) publicada en el PDF y aun sin reporte de operaciones.")

    return {"comparadas": len(comunes), "coincidencias": coincidencias,
            "diferencias": len(diferencias), "porcentaje": pct}


def validar_compra_venta(filas_tco, serie_html, serie_pdf, solapados):
    porhtml = {f["vigencia"]: f for f in filas_tco}
    comparadas = difs = spread_malo = omitidas = 0

    for fecha, h in sorted(serie_html.items()):
        f = porhtml.get(fecha)
        if f is None or not h["compra"] or not h["venta"]:
            continue
        if dt.date.fromisoformat(fecha).month in solapados and serie_pdf[fecha]["oficial"]:
            omitidas += 1
            continue
        comparadas += 1
        if abs(float(f["tco_compra"]) - float(h["compra"])) > TOL_TCO or \
                abs(float(f["tco_venta"]) - float(h["venta"])) > TOL_TCO:
            difs += 1
            error(f"{fecha} compra/venta calculada={f['tco_compra']}/{f['tco_venta']} "
                  f"vs HTML del BCB={h['compra']}/{h['venta']}")

    for f in filas_tco:
        if Decimal(f["tco_venta"]) - Decimal(f["tco_compra"]) != SPREAD:
            spread_malo += 1
            error(f"{f['vigencia']}: spread venta-compra = "
                  f"{Decimal(f['tco_venta']) - Decimal(f['tco_compra'])}, se esperaba {SPREAD}")

    print()
    print("--- Validacion compra/venta (contra la tabla HTML del BCB) ---")
    print(f"    fechas comparadas        : {comparadas}")
    print(f"    diferencias              : {difs}")
    print(f"    spread distinto de {SPREAD}  : {spread_malo}")
    print(f"    omitidas por solapamiento: {omitidas}")
    if difs == 0 and spread_malo == 0:
        ok(f"compra/venta validadas contra el HTML del BCB ({comparadas} fechas)")

    return {"comparadas": comparadas, "diferencias": difs + spread_malo}


def validar_operaciones(detalle, bancos):
    agr, suma_tc, tot_tc = {}, {}, {}
    for d in detalle:
        if d["banco"] == TOTAL_BANCOS:
            tot_tc[(d["fecha_corte"], d["tc"])] = (d["_n"], d["_m"])
            continue
        a = agr.setdefault((d["fecha_corte"], d["banco"]), {"n": 0.0, "m": 0.0, "tcm": 0.0, "k": 0})
        a["n"] += d["_n"] or 0
        a["m"] += d["_m"] or 0
        a["tcm"] += d["_tc"] * (d["_m"] or 0)
        a["k"] += 1
        s = suma_tc.setdefault((d["fecha_corte"], d["tc"]), [0.0, 0.0, 0])
        s[0] += d["_n"] or 0
        s[1] += d["_m"] or 0
        s[2] += 1

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
            aviso(f"{corte} {banco}: la fuente no publica el TOTAL; no se valida.")
        else:
            if abs(a["n"] - p["_n"]) > 1e-9:
                dif_n += 1
                error(f"{corte} {banco}: operaciones detalle={a['n']:.0f} vs agregado={p['_n']:.0f}")
            tol = TOL_REDONDEO_CELDA * a["k"] + 1e-6
            if abs(a["m"] - p["_m"]) > tol:
                dif_m += 1
                error(f"{corte} {banco}: monto detalle={a['m']:.0f} vs agregado={p['_m']:.0f} "
                      f"(tolerancia {tol:.1f})")
        if p["_tco"] is not None and a["m"] > 0:
            pond = a["tcm"] / a["m"]
            if abs(pond - p["_tco"]) > TOL_PONDERADO:
                dif_pond += 1
                error(f"{corte} {banco}: TCO ponderado={pond:.6f} vs publicado={p['_tco']:.2f} "
                      f"dif={pond - p['_tco']:+.6f}")

    for k, (n, m, celdas) in sorted(suma_tc.items()):
        t = tot_tc.get(k)
        if t is None:
            continue
        if t[0] is not None and abs(n - t[0]) > 1e-9:
            dif_tc += 1
            error(f"{k[0]} TC={k[1]}: suma bancos N={n:.0f} vs TOTAL BANCOS={t[0]:.0f}")
        if t[1] is not None and abs(m - t[1]) > TOL_REDONDEO_CELDA * celdas + 1e-6:
            dif_tc += 1
            error(f"{k[0]} TC={k[1]}: suma bancos monto={m:.0f} vs TOTAL BANCOS={t[1]:.0f}")

    print()
    print("--- Validacion interna de operaciones ---")
    print(f"    pares fecha x banco comparados : {comparadas}")
    print(f"    diferencias en N operaciones   : {dif_n}")
    print(f"    diferencias en monto USD       : {dif_m}   (tolerancia {TOL_REDONDEO_CELDA}/celda)")
    print(f"    diferencias en TCO ponderado   : {dif_pond} (tolerancia {TOL_PONDERADO})")
    print(f"    filas TC vs TOTAL BANCOS       : {dif_tc}")
    print(f"    agregados no publicados        : {sin_dato}")
    if dif_n == dif_m == dif_pond == dif_tc == 0:
        ok(f"detalle.csv y bancos.csv validados ({comparadas} pares fecha x banco)")

    return {"comparadas": comparadas, "dif_n": dif_n, "dif_m": dif_m,
            "dif_pond": dif_pond, "dif_tc": dif_tc}


def validar_estructura(tco, bancos, detalle) -> bool:
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


def actualizar_raw(nuevos: dict) -> tuple[bool, int]:
    miembros: dict[str, bytes] = {}
    huellas: dict[str, str] = {}
    manifiesto_previo = None
    if os.path.exists(RAW_ZIP):
        try:
            with zipfile.ZipFile(RAW_ZIP) as z:
                nombres = z.namelist()
                for n in nombres:
                    if n != "manifest.json":
                        miembros[n] = z.read(n)
                if "manifest.json" in nombres:
                    manifiesto_previo = z.read("manifest.json")
                    previo = json.loads(manifiesto_previo.decode("utf-8"))
                    huellas = {n: d["huella"] for n, d in previo.items() if d.get("huella")}
        except (zipfile.BadZipFile, ValueError, KeyError):
            error("datos/raw.zip esta corrupto; se regenera desde cero.")
            miembros, huellas, manifiesto_previo = {}, {}, None

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

    if manifiesto_previo == manifiesto and os.path.exists(RAW_ZIP):
        return False, len(miembros)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for nombre in sorted(list(miembros) + ["manifest.json"]):
            datos = manifiesto if nombre == "manifest.json" else miembros[nombre]
            info = zipfile.ZipInfo(nombre, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, datos)
    with open(RAW_ZIP, "wb") as fh:
        fh.write(buf.getvalue())
    return True, len(miembros)


def _serif() -> str:
    from matplotlib import font_manager
    disponibles = {f.name for f in font_manager.fontManager.ttflist}
    for nombre in ("Latin Modern Roman", "CMU Serif", "Nimbus Roman", "Liberation Serif",
                   "DejaVu Serif", "Times New Roman"):
        if nombre in disponibles:
            return nombre
    return "serif"


def generar_estado(payload: dict) -> bool:
    huella = json.dumps({k: v for k, v in payload.items() if k != "corrida"},
                        sort_keys=True, ensure_ascii=False)
    marca = os.path.join(ESTADO, ".huella")
    previa = None
    if os.path.exists(marca):
        with open(marca, "r", encoding="utf-8") as fh:
            previa = fh.read()
    if previa == huella and os.path.exists(PNG_ESTADO) and not os.environ.get("TCO_SELLAR"):
        ok("status.png sin cambios")
        return False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    CREMA, VERDE, ORO, TINTA, ROJO = "#F7F2E4", "#14452F", "#B08D4F", "#1E1B16", "#8E2B20"
    acento = VERDE if payload["resultado"] == "EXITOSO" else ROJO
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
    ax.text(50, 91.5, "T C O — B C B", size=27, color=acento, **T)
    ax.text(50, 88.4, "Certificado de estado de la ultima corrida", size=11.5,
            color=TINTA, style="italic", **T)
    ax.plot([26, 74], [86.6, 86.6], color=ORO, lw=1.0)
    ax.text(50, 83.4, "RESULTADO GENERAL", size=9, color=TINTA, **T)
    ax.add_patch(Rectangle((31, 76.4), 38, 5.9, fill=False, ec=acento, lw=1.4))
    ax.text(50, 77.9, payload["resultado"], size=21, color=acento, **T)

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

    y = bloque(71.5, "Informacion de la corrida", [
        ("Ultima corrida", payload["corrida"], None),
        ("Ultima fecha BCB disponible", payload["ultima_fecha"], None),
        ("Ultimo TCO (compra / venta)", payload["ultimo_tco"], None),
        ("Ultimo corte de operaciones", payload["ultimo_corte"], None),
        ("Actualizacion programada", "22:00 (Bolivia)", None),
    ])
    y = bloque(y - 1.8, "Archivos", [
        ("datos/tco.csv", payload["tco"], col(payload["tco"])),
        ("datos/bancos.csv", payload["bancos"], col(payload["bancos"])),
        ("datos/detalle.csv", payload["detalle"], col(payload["detalle"])),
        ("datos/raw.zip", payload["raw"], col(payload["raw"])),
    ])
    y = bloque(y - 1.8, "Validaciones", [
        ("Validacion CSV vs PDF", payload["val_pdf"], col(payload["val_pdf"])),
        ("Validacion compra / venta", payload["val_cv"], col(payload["val_cv"])),
        ("Validacion de operaciones", payload["val_ops"], col(payload["val_ops"])),
        ("Estructura y duplicados", payload["val_est"], col(payload["val_est"])),
    ])
    ax.text(50, y - 1.0, payload["detalle_texto"], size=8.5, color=TINTA, style="italic", **T)

    ax.plot([26, 74], [9.4, 9.4], color=ORO, lw=0.8)
    ax.text(50, 7.2, "Documento generado automaticamente por TCO-BCB", size=8.5, color=TINTA, **T)
    ax.text(50, 5.2, "Proyecto independiente basado en fuentes publicas del Banco Central de Bolivia",
            size=7, color="#6B6459", **T)

    fig.savefig(PNG_ESTADO, facecolor=CREMA)
    plt.close(fig)
    with open(marca, "w", encoding="utf-8") as fh:
        fh.write(huella)
    ok("status.png generado")
    return True


def ejecutar(reconstruir: bool = False) -> int:
    os.makedirs(DATOS, exist_ok=True)
    os.makedirs(ESTADO, exist_ok=True)
    ahora = dt.datetime.now(HUSO_BOLIVIA)

    print("=" * 78)
    print(f"TCO-BCB  |  corrida {ahora:%Y-%m-%d %H:%M} (Bolivia)"
          + ("  |  RECONSTRUCCION COMPLETA" if reconstruir else ""))
    print("=" * 78)

    if reconstruir:
        for ruta in (CSV_TCO, CSV_BANCOS, CSV_DETALLE):
            if os.path.exists(ruta):
                os.remove(ruta)

    print(f"[..] Estado local: ultima vigencia={leer_ultima_fecha(CSV_TCO, 'vigencia') or 'sin datos'}"
          f" | ultimo corte={leer_ultima_fecha(CSV_BANCOS, 'fecha_corte') or 'sin datos'}")

    try:
        descargar(URL_SERIE)
        ok("Fuente BCB accesible")
    except Exception as exc:  # noqa: BLE001
        error(f"No se pudo acceder a la pagina oficial de la serie: {exc}")

    crudos: dict[str, tuple[bytes, str | None]] = {}
    serie_pdf, serie_html, solapados = {}, {}, set()
    for anio in sorted({FECHA_INICIO.year, ahora.year}):
        pdf = descargar(URL_PDF.format(anio=anio))
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError(f"La respuesta de pdf.php?anio={anio} no es un PDF.")
        serie, huella, solap = parsear_pdf_anual(pdf, anio)
        serie_pdf.update(serie)
        solapados |= solap
        crudos[f"historico/{anio}.pdf"] = (pdf, huella)

        htm = descargar(URL_HTML.format(anio=anio))
        serie_html.update(parsear_html_anual(htm, anio))
        crudos[f"tco/{anio}.html"] = (htm, None)
    ok(f"PDF anual leido: {len(serie_pdf)} dias | HTML anual leido: {len(serie_html)} dias")

    hasta = (ahora.date() + dt.timedelta(days=3)).isoformat()
    fuentes = []
    for etiqueta, plantilla in URL_OPERACIONES:
        try:
            crudo = descargar(plantilla.format(desde=PRIMER_CORTE.isoformat(), hasta=hasta))
            fuentes.append((etiqueta, crudo, parsear_operaciones(crudo)))
        except Exception as exc:  # noqa: BLE001
            aviso(f"La fuente de operaciones '{etiqueta}' no respondio: {exc}")
    if not fuentes:
        raise RuntimeError("Ninguna de las fuentes de operaciones respondio.")

    detalle, bancos, tco_ops, cortes, orden_bancos = combinar_operaciones(
        [(etiqueta, resultado) for etiqueta, _, resultado in fuentes])
    ok(f"operaciones leidas de {len(fuentes)} fuente(s): {len(cortes)} fechas de corte")

    ult_corte = cortes[-1] if cortes else None
    filas_tco = construir_tco(serie_pdf, tco_ops)
    ult = filas_tco[-1] if filas_tco else None
    print(f"[..] Estado BCB  : ultima vigencia={ult['vigencia'] if ult else None} "
          f"| ultimo corte={ult_corte}")

    detalle.sort(key=lambda d: (d["fecha_corte"], d["banco"], d["_tc"]))
    bancos.sort(key=lambda b: (b["fecha_corte"], b["banco"] == TOTAL_BANCOS, b["banco"]))
    est_ok = validar_estructura(filas_tco, bancos, detalle)

    reales = [b for b in orden_bancos if b != TOTAL_BANCOS]
    campos_det, filas_det = empaquetar_detalle(detalle, reales)
    campos_ban, filas_ban = agregar_bancos(bancos, orden_bancos)

    cambio_tco = escribir_csv(CSV_TCO, ["fecha_corte", "vigencia", "tco_compra", "tco_venta"],
                              filas_tco)
    cambio_ban = escribir_csv(CSV_BANCOS, campos_ban, filas_ban)
    cambio_det = escribir_csv(CSV_DETALLE, campos_det, filas_det)

    for nombre, cambio, n in (("tco.csv", cambio_tco, len(filas_tco)),
                              ("bancos.csv", cambio_ban, len(filas_ban)),
                              ("detalle.csv", cambio_det, len(filas_det))):
        ok(f"{nombre} {'actualizado' if cambio else 'sin cambios'} ({n} filas)")

    hay_novedad = cambio_tco or cambio_ban or cambio_det
    if not hay_novedad:
        print("NO_NEW_DATA")

    res_pdf = validar_csv_vs_pdf(tco_ops, serie_pdf)
    res_cv = validar_compra_venta(filas_tco, serie_html, serie_pdf, solapados)
    res_ops = validar_operaciones(detalle, bancos)

    for etiqueta, crudo, (_, _, _, cortes_fuente, _) in fuentes:
        lineas = crudo.decode("utf-8-sig", "replace").splitlines()
        i_cab = next((i for i, l in enumerate(lineas)
                      if l.lower().startswith('"fecha de corte"')), None)
        encabezado = lineas[i_cab:i_cab + 2] if i_cab is not None else []
        por_corte: dict[str, list[str]] = {c: [] for c in cortes_fuente}
        for l in lineas:
            c = l.split(";", 1)[0]
            if c in por_corte:
                por_corte[c].append(l)
        carpeta = "operaciones" if etiqueta == "publico" else f"operaciones_{etiqueta}"
        for corte, filas in por_corte.items():
            cuerpo = "\n".join(encabezado + filas) + "\n"
            crudos[f"{carpeta}/{corte}.csv"] = (cuerpo.encode("utf-8"), None)

    cambio_raw, n_raw = actualizar_raw(crudos)
    ok(f"raw.zip {'actualizado' if cambio_raw else 'sin cambios'} ({n_raw} archivos)")

    val_ops_ok = res_ops["dif_n"] == res_ops["dif_m"] == res_ops["dif_pond"] == res_ops["dif_tc"] == 0
    resultado = "EXITOSO" if not ERRORES else "ERROR"

    generar_estado({
        "resultado": resultado,
        "corrida": f"{ahora:%Y-%m-%d %H:%M} (Bolivia)",
        "ultima_fecha": ult["vigencia"] if ult else "sin datos",
        "ultimo_tco": f"{ult['tco_compra']} / {ult['tco_venta']} Bs/USD" if ult else "sin datos",
        "ultimo_corte": ult_corte or "sin datos",
        "tco": "OK" if cambio_tco else "SIN CAMBIOS",
        "bancos": "OK" if cambio_ban else "SIN CAMBIOS",
        "detalle": "OK" if cambio_det else "SIN CAMBIOS",
        "raw": "OK" if cambio_raw else "SIN CAMBIOS",
        "val_pdf": "OK" if res_pdf["diferencias"] == 0 else "DIFERENCIAS",
        "val_cv": "OK" if res_cv["diferencias"] == 0 else "DIFERENCIAS",
        "val_ops": "OK" if val_ops_ok else "DIFERENCIAS",
        "val_est": "OK" if est_ok else "DIFERENCIAS",
        "detalle_texto": (f"CSV vs PDF: {res_pdf['coincidencias']}/{res_pdf['comparadas']} fechas "
                          f"({res_pdf['porcentaje']:.2f}%)   |   "
                          f"compra/venta: {res_cv['comparadas']} fechas   |   "
                          f"{res_ops['comparadas']} pares fecha x banco"),
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
            generar_estado({
                "resultado": "ERROR",
                "corrida": f"{dt.datetime.now(HUSO_BOLIVIA):%Y-%m-%d %H:%M} (Bolivia)",
                "ultima_fecha": leer_ultima_fecha(CSV_TCO, "vigencia") or "sin datos",
                "ultimo_tco": "sin datos", "ultimo_corte": "sin datos",
                "tco": "ERROR", "bancos": "ERROR", "detalle": "ERROR", "raw": "ERROR",
                "val_pdf": "ERROR", "val_cv": "ERROR", "val_ops": "ERROR", "val_est": "ERROR",
                "detalle_texto": f"{type(exc).__name__}: {str(exc)[:90]}",
            })
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
