"""
pdf_obsidian.py
----------------
Conversor genérico de PDF -> Markdown pensado para Obsidian.

No depende de reglas escritas para un documento concreto: deduce la estructura
a partir de la tipografía y la posición del texto en cada página.

Qué hace:
  * Títulos (#, ##, ###...) según el tamaño de letra y las líneas en negrita.
  * Párrafos reconstruidos (une líneas cortadas y palabras con guion al final).
  * Listas con viñetas y numeradas, con sangría anidada.
  * Negritas y cursivas dentro del texto.
  * Tablas con bordes (pdfplumber) y tablas sin bordes (columnas alineadas).
  * Documentos a dos columnas (lee la columna izquierda y luego la derecha).
  * Quita encabezados / pies de página repetidos y números de página.
  * Extrae imágenes a una carpeta de adjuntos y las enlaza con ![[...]].
  * Escapa caracteres que Obsidian interpretaría mal ($, #etiqueta, <, *...).
  * Añade propiedades (frontmatter YAML) para Obsidian.

Uso rápido:
    from pdf_obsidian import convertir_pdf
    res = convertir_pdf("documento.pdf", carpeta_imagenes="adjuntos")
    print(res.markdown)
"""

from __future__ import annotations

import math
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pdfplumber

# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

LIGADURAS = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u00ad": "",  # guion suave
    "\x00": "",
    "\u200b": "",
    "\uffff": "",
    "\ufffe": "",
    "\ufeff": "",
}

VINETAS = "•●▪◦‣■□▫◆◇►▶➢➤✓✔✗❖⦿⁃∙·○◉➔→-–—*"
RE_VINETA = re.compile(r"^[" + re.escape(VINETAS) + r"]$")
RE_VINETA_PEGADA = re.compile(r"^([" + re.escape(VINETAS.replace("-", "").replace("–", "").replace("—", "")) + r"])(\S.*)$")
RE_NUMERO = re.compile(r"^(\(?\d{1,3}[.)]|\d{1,2}\.\d{1,2}[.)]?|[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)])$")
RE_NUM_PAGINA = re.compile(
    r"^(p[áa]g(ina)?\.?|page|p\.)?\s*[-–]?\s*\d{1,4}\s*[-–]?\s*((de|of|/)\s*\d{1,4})?$",
    re.IGNORECASE,
)
RE_NEGRITA = re.compile(r"bold|black|heavy|semibold|demibold|demi|,b$|-b$|\bbd\b", re.IGNORECASE)
RE_CURSIVA = re.compile(r"italic|oblique|,i$|-it$|\bit\b", re.IGNORECASE)
RE_CID = re.compile(r"\(cid:\d+\)")


def limpiar_texto(t: str) -> str:
    for a, b in LIGADURAS.items():
        t = t.replace(a, b)
    return t


def es_caracter_privado(t: str) -> bool:
    """Viñetas de fuentes Symbol/Wingdings que llegan como caracteres de uso privado."""
    return len(t) == 1 and 0xE000 <= ord(t) <= 0xF8FF


def escapar_md(texto: str) -> str:
    """Escapa caracteres que Markdown/Obsidian convertirían en formato."""
    texto = texto.replace("\\", "\\\\")
    texto = texto.replace("*", "\\*")
    texto = texto.replace("$", "\\$")          # Obsidian: fórmulas LaTeX
    texto = texto.replace("==", "\\=\\=")      # Obsidian: resaltado
    texto = texto.replace("%%", "\\%\\%")      # Obsidian: comentarios
    texto = texto.replace("[[", "\\[\\[")      # Obsidian: wikilinks
    texto = texto.replace("`", "\\`")
    texto = re.sub(r"(?<![\w])_|_(?![\w])", r"\\_", texto)
    texto = re.sub(r"<(?=[A-Za-z/!])", "&lt;", texto)
    texto = re.sub(r"(^|[\s(\[])#(?=[^\s#])", r"\1\\#", texto)  # evita #etiquetas
    texto = texto.replace("~~", "\\~\\~")
    return texto


def escapar_inicio_linea(texto: str) -> str:
    """Evita que un párrafo empiece pareciendo título, cita o lista."""
    if re.match(r"^#{1,6}(\s|$)", texto):
        return "\\" + texto
    if texto.startswith(">"):
        return "\\" + texto
    m = re.match(r"^(\d{1,9})([.)])(\s)", texto)
    if m:
        return m.group(1) + "\\" + m.group(2) + texto[len(m.group(1)) + 1:]
    if re.match(r"^[-+](\s|$)", texto):
        return "\\" + texto
    return texto


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------

@dataclass
class Palabra:
    texto: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    negrita: bool
    cursiva: bool


@dataclass
class Linea:
    palabras: list
    pagina: int
    columna: int = 0          # 0 = ancho completo, 1 = izquierda, 2 = derecha
    tipo: str = "texto"       # texto | titulo | lista
    nivel: int = 0

    @property
    def x0(self): return min(p.x0 for p in self.palabras)
    @property
    def x1(self): return max(p.x1 for p in self.palabras)
    @property
    def top(self): return min(p.top for p in self.palabras)
    @property
    def bottom(self): return max(p.bottom for p in self.palabras)

    @property
    def texto(self):
        return " ".join(p.texto for p in self.palabras)

    @property
    def size(self):
        pesos = Counter()
        for p in self.palabras:
            pesos[round(p.size * 2) / 2] += len(p.texto)
        return pesos.most_common(1)[0][0] if pesos else 0

    @property
    def negrita(self):
        utiles = [p for p in self.palabras if re.search(r"\w", p.texto)]
        return bool(utiles) and all(p.negrita for p in utiles)


@dataclass
class Bloque:
    tipo: str                 # titulo | parrafo | lista | tabla | imagen | nota
    pagina: int
    top: float = 0.0
    nivel: int = 0
    palabras: list = field(default_factory=list)
    marcador: str = ""
    filas: list = field(default_factory=list)
    texto: str = ""
    x0: float = 0.0
    columna: int = 0


@dataclass
class ResultadoConversion:
    markdown: str
    titulo: str
    paginas: int
    imagenes: list            # rutas absolutas de imágenes guardadas
    paginas_sin_texto: list   # páginas que parecen escaneadas
    imagenes_ocr: int = 0     # imágenes de las que se extrajo texto
    paginas_ocr: list = field(default_factory=list)  # páginas escaneadas leídas con OCR


# ---------------------------------------------------------------------------
# Extracción por página
# ---------------------------------------------------------------------------

def _centro_dentro(p, bbox):
    cx = (p["x0"] + p["x1"]) / 2
    cy = (p["top"] + p["bottom"]) / 2
    return bbox[0] - 1 <= cx <= bbox[2] + 1 and bbox[1] - 1 <= cy <= bbox[3] + 1


def _limpiar_celda(c) -> str:
    if c is None:
        return ""
    c = limpiar_texto(str(c))
    c = RE_CID.sub("", c)
    c = re.sub(r"(\w)-\n(\w)", r"\1\2", c)
    c = re.sub(r"\s*\n\s*", " ", c)
    return c.strip()


def _normalizar_tabla(filas: list[list]) -> list[list[str]]:
    filas = [[_limpiar_celda(c) for c in f] for f in filas]
    filas = [f for f in filas if any(f)]
    if not filas:
        return []
    ncol = max(len(f) for f in filas)
    filas = [f + [""] * (ncol - len(f)) for f in filas]
    # quitar columnas completamente vacías (celdas combinadas)
    llenas = [i for i in range(ncol) if any(f[i] for f in filas)]
    filas = [[f[i] for i in llenas] for f in filas]
    return filas


def _tablas_con_bordes(page) -> list[tuple[tuple, list[list[str]]]]:
    resultado = []
    try:
        encontradas = page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
    except Exception:
        return resultado
    for t in encontradas:
        try:
            filas = _normalizar_tabla(t.extract())
        except Exception:
            continue
        if len(filas) >= 2 and len(filas[0]) >= 2:
            resultado.append((t.bbox, filas))
    return resultado


def _palabras_pagina(page, excluir_bboxes) -> list[Palabra]:
    try:
        crudas = page.extract_words(
            extra_attrs=["size", "fontname"], keep_blank_chars=False, x_tolerance=2, y_tolerance=3
        )
    except Exception:
        crudas = page.extract_words()
    palabras = []
    W, H = page.width, page.height
    for w in crudas:
        if any(_centro_dentro(w, b) for b in excluir_bboxes):
            continue
        if w["x1"] < 0 or w["x0"] > W or w["bottom"] < 0 or w["top"] > H:
            continue
        t = limpiar_texto(w["text"])
        if RE_CID.fullmatch(t) or es_caracter_privado(t):
            t = "•"
        else:
            t = RE_CID.sub("", t)
            t = "".join("•" if es_caracter_privado(ch) else ch for ch in t)
        if not t.strip():
            continue
        fuente = w.get("fontname", "") or ""
        fuente_base = fuente.split("+")[-1]
        palabras.append(Palabra(
            texto=t, x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
            size=float(w.get("size", 10) or 10),
            negrita=bool(RE_NEGRITA.search(fuente_base)),
            cursiva=bool(RE_CURSIVA.search(fuente_base)),
        ))
    return palabras


def _agrupar_filas(palabras: list[Palabra]) -> list[list[Palabra]]:
    """Agrupa palabras que comparten renglón."""
    filas: list[list[Palabra]] = []
    for p in sorted(palabras, key=lambda p: (p.top, p.x0)):
        medio = (p.top + p.bottom) / 2
        colocada = False
        for fila in reversed(filas[-3:]):
            ftop = min(q.top for q in fila)
            fbot = max(q.bottom for q in fila)
            alto = max(fbot - ftop, 1)
            if ftop - 0.2 * alto <= medio <= fbot + 0.2 * alto and abs(p.top - ftop) < 0.8 * alto:
                fila.append(p)
                colocada = True
                break
        if not colocada:
            filas.append([p])
    for f in filas:
        f.sort(key=lambda p: p.x0)
    filas.sort(key=lambda f: min(p.top for p in f))
    return filas


def _detectar_canal(palabras: list[Palabra], ancho: float) -> Optional[float]:
    """Busca un canal vertical vacío que separe dos columnas de texto."""
    if len(palabras) < 40:
        return None
    # recorrer el centro de la página buscando la franja vacía más ancha
    tolerancia = 1 if len(palabras) > 150 else 0
    mejor, mejor_ancho = None, 0.0
    inicio = None
    x = ancho * 0.25
    while x <= ancho * 0.75 + 1:
        libre = sum(1 for p in palabras if p.x0 < x < p.x1) <= tolerancia
        if libre and inicio is None:
            inicio = x
        if (not libre or x + 1 > ancho * 0.75 + 1) and inicio is not None:
            fin = x
            if fin - inicio > mejor_ancho:
                mejor, mejor_ancho = (inicio + fin) / 2, fin - inicio
            inicio = None
        x += 1
    if mejor is None:
        return None
    izq = sum(1 for p in palabras if p.x1 <= mejor)
    der = sum(1 for p in palabras if p.x0 >= mejor)
    if izq < len(palabras) * 0.2 or der < len(palabras) * 0.2:
        return None
    tam_medio = statistics.median(p.size for p in palabras)
    if mejor_ancho < max(9.0, 0.9 * tam_medio):
        return None
    # comprobar que realmente hay renglones paralelos (no sólo un bloque lateral)
    filas = _agrupar_filas(palabras)
    paralelas = sum(
        1 for f in filas if any(p.x1 <= mejor for p in f) and any(p.x0 >= mejor for p in f)
        and not any(p.x0 < mejor < p.x1 for p in f)
    )
    if paralelas < max(5, len(filas) * 0.3):
        return None
    return mejor


def _lineas_pagina(palabras: list[Palabra], num_pagina: int, ancho: float) -> list[Linea]:
    filas = _agrupar_filas(palabras)
    canal = _detectar_canal(palabras, ancho)
    if canal is None:
        return [Linea(palabras=f, pagina=num_pagina, columna=0) for f in filas]

    # Lectura a dos columnas: los renglones que cruzan el canal cortan secciones.
    salida: list[Linea] = []
    izq: list[Linea] = []
    der: list[Linea] = []

    def vaciar():
        salida.extend(izq)
        salida.extend(der)
        izq.clear()
        der.clear()

    for f in filas:
        if any(p.x0 < canal < p.x1 for p in f):
            vaciar()
            salida.append(Linea(palabras=f, pagina=num_pagina, columna=0))
            continue
        pi = [p for p in f if p.x1 <= canal]
        pd = [p for p in f if p.x0 >= canal]
        if pi:
            izq.append(Linea(palabras=pi, pagina=num_pagina, columna=1))
        if pd:
            der.append(Linea(palabras=pd, pagina=num_pagina, columna=2))
    vaciar()
    return salida


def _recortar(page, bbox, resolucion):
    return page.crop(bbox).to_image(resolution=resolucion).original


def _procesar_imagenes(page, num_pagina, carpeta, prefijo, contador_inicio, palabras_nativas,
                       motor_ocr=None, minimo=40):
    """Guarda las imágenes de la página y, si hay motor OCR, lee el texto que contienen.

    Devuelve [(top, x0, nombre_png | None, ruta | None, markdown_ocr), ...]
    """
    salida = []
    W, H = page.width, page.height
    n = contador_inicio
    vistos = set()
    for img in page.images:
        x0, top = max(0, img["x0"]), max(0, img["top"])
        x1, bottom = min(W, img["x1"]), min(H, img["bottom"])
        if x1 - x0 < minimo or bottom - top < minimo:
            continue
        clave = (round(x0), round(top), round(x1), round(bottom))
        if clave in vistos:
            continue
        vistos.add(clave)
        bbox = (x0, top, x1, bottom)
        nombre = ruta = None
        if carpeta:
            try:
                os.makedirs(carpeta, exist_ok=True)
                n += 1
                nombre = f"{prefijo}-p{num_pagina}-{n}.png"
                ruta = os.path.join(carpeta, nombre)
                _recortar(page, bbox, 150).save(ruta)
            except Exception:
                nombre = ruta = None
        md_ocr = ""
        # Si hay texto real del PDF encima de la imagen, es sólo un fondo: no hace falta OCR.
        encima = sum(1 for p in palabras_nativas
                     if x0 <= (p.x0 + p.x1) / 2 <= x1 and top <= (p.top + p.bottom) / 2 <= bottom)
        if motor_ocr is not None and encima < 5 and (x1 - x0) >= 60 and (bottom - top) >= 20:
            try:
                md_ocr = ocr_a_markdown(_recortar(page, bbox, 300), motor_ocr, titulos=False)
            except Exception as e:
                md_ocr = f"*(no se pudo aplicar OCR a esta imagen: {e})*"
        if nombre or md_ocr:
            salida.append((top, x0, nombre, ruta, md_ocr))
    return salida


# ---------------------------------------------------------------------------
# OCR -> Markdown estructurado
# ---------------------------------------------------------------------------

VINETAS_OCR = {"e", "o", "¢", "©", "®", "«", "»", "*", "°", "•", "·", "-", "–", "—", ">", "+", "=", "a", "@", "§", "¤"}


def _parece_texto(palabras: list[str]) -> bool:
    """Descarta el "ruido" que el OCR produce en fotos o gráficos sin texto."""
    if len(palabras) < 3:
        return False
    buenas = [w for w in palabras if re.search(r"[A-Za-zÀ-ÿ0-9]{2,}", w)]
    return len(buenas) >= 3 and len(buenas) / len(palabras) >= 0.5


def ocr_a_markdown(img, motor, titulos: bool = True) -> str:
    """Aplica OCR a una imagen PIL (renderizada a 300 ppp) y devuelve Markdown estructurado."""
    if getattr(motor, "devuelve_markdown", False):
        md = motor.markdown(img)
        if not titulos:
            md = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", md, flags=re.M)
        return md.strip()

    lineas_px = motor.lineas(img)
    if not _parece_texto([w["text"] for ln in lineas_px for w in ln]):
        return ""
    esc = 72.0 / 300.0  # píxeles -> puntos
    palabras = []
    for ln in lineas_px:
        alto = (max(w["bottom"] for w in ln) - min(w["top"] for w in ln)) * esc
        for w in ln:
            t = limpiar_texto(str(w["text"])).strip()
            if not t:
                continue
            palabras.append(Palabra(t, w["x0"] * esc, w["x1"] * esc, w["top"] * esc, w["bottom"] * esc,
                                    size=round(max(alto * 0.8, 4), 1), negrita=False, cursiva=False))
    if not palabras:
        return ""
    ancho = max(p.x1 for p in palabras) + 10
    lineas = _lineas_pagina(palabras, 0, ancho)
    # El OCR suele leer las viñetas como "e", "¢", "©", "«"...: si el primer "carácter"
    # está separado del resto por un hueco amplio, es una viñeta.
    for l in lineas:
        if len(l.palabras) > 1 and l.palabras[0].texto in VINETAS_OCR:
            a, b = l.palabras[0], l.palabras[1]
            if b.x0 - a.x1 > 0.45 * b.size:
                a.texto = "•"
    # Viñetas que el OCR omitió: renglón alineado con el texto de los elementos de lista
    # vecinos (y el anterior no llega al margen, es decir, no es texto que continúa).
    derecha = max(l.x1 for l in lineas)
    izquierda = min(l.x0 for l in lineas)
    def es_item(l):
        return len(l.palabras) > 1 and l.palabras[0].texto == "•"
    for k, l in enumerate(lineas):
        if es_item(l) or l.x0 - izquierda < 8:
            continue
        vecinos = [lineas[j] for j in (k - 1, k + 1) if 0 <= j < len(lineas) and es_item(lineas[j])]
        if not any(abs(v.palabras[1].x0 - l.x0) < 4 for v in vecinos):
            continue
        ant = lineas[k - 1] if k > 0 else None
        if ant is not None and es_item(ant) and ant.x1 > derecha - 0.25 * (derecha - izquierda):
            continue  # el anterior es largo: esto es su continuación
        p0 = l.palabras[0]
        l.palabras.insert(0, Palabra("•", p0.x0 - p0.size, p0.x0 - p0.size * 0.6, p0.top, p0.bottom,
                                     p0.size, False, False))
    cuerpo, niveles, interlineado = _estadisticas({0: lineas}, factor_titulo=1.25,
                                                  tolerancia_rel=0.15)
    if not titulos:
        niveles = {}
    items = _detectar_tablas_alineadas(lineas)
    bloques = _construir_bloques(items, cuerpo, niveles, interlineado, titulos_negrita=titulos)
    return _render(bloques).strip()


def callout(tipo: str, titulo: str, contenido: str = "", plegado: bool = False) -> str:
    """Bloque de aviso de Obsidian:  > [!tipo] título  + contenido citado."""
    cab = f"> [!{tipo}]{'-' if plegado else ''} {titulo}"
    lineas = [("> " + l) if l.strip() else ">" for l in contenido.strip().split("\n")] if contenido.strip() else []
    return "\n".join([cab] + lineas)


# ---------------------------------------------------------------------------
# Encabezados y pies repetidos
# ---------------------------------------------------------------------------

def _clave_margen(texto: str) -> str:
    return re.sub(r"\d+", "#", texto.lower()).strip()


def _quitar_encabezados_pies(lineas_por_pagina: dict, alturas: dict) -> None:
    n = len(lineas_por_pagina)
    conteo = Counter()
    for pg, lineas in lineas_por_pagina.items():
        H = alturas[pg]
        vistas = set()
        for l in lineas:
            if l.top < H * 0.10 or l.bottom > H * 0.90:
                vistas.add(_clave_margen(l.texto))
        conteo.update(vistas)
    umbral = max(2, math.ceil(n * 0.4))
    for pg, lineas in lineas_por_pagina.items():
        H = alturas[pg]
        nuevas = []
        for l in lineas:
            en_margen = l.top < H * 0.10 or l.bottom > H * 0.90
            if en_margen and (
                (n >= 2 and conteo[_clave_margen(l.texto)] >= umbral)
                or RE_NUM_PAGINA.match(l.texto.strip())
            ):
                continue
            nuevas.append(l)
        lineas_por_pagina[pg] = nuevas


# ---------------------------------------------------------------------------
# Tablas sin bordes (columnas alineadas)
# ---------------------------------------------------------------------------

def _celdas_de_linea(l: Linea) -> list[list[Palabra]]:
    celdas = [[l.palabras[0]]]
    for i in range(1, len(l.palabras)):
        prev, cur = l.palabras[i - 1], l.palabras[i]
        hueco = cur.x0 - prev.x1
        umbral = max(10.0, 1.3 * max(prev.size, cur.size))
        es_marcador = i == 1 and (RE_VINETA.match(prev.texto) or RE_NUMERO.match(prev.texto))
        if hueco > umbral and not es_marcador:
            celdas.append([cur])
        else:
            celdas[-1].append(cur)
    return celdas


def _columnas_por_solape(intervalos: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cols = []
    for a, b in sorted(intervalos):
        if cols and a <= cols[-1][1] + 2:
            cols[-1] = (cols[-1][0], max(cols[-1][1], b))
        else:
            cols.append((a, b))
    return cols


def _detectar_tablas_alineadas(lineas: list[Linea]) -> list:
    """Devuelve una lista mezclando Linea y Bloque(tipo='tabla')."""
    resultado = []
    i = 0
    n = len(lineas)
    while i < n:
        l = lineas[i]
        celdas = _celdas_de_linea(l)
        if len(celdas) < 2:
            resultado.append(l)
            i += 1
            continue
        # construir la corrida de renglones tabulares
        corrida = [(l, celdas)]
        j = i + 1
        while j < n:
            sig = lineas[j]
            prev = corrida[-1][0]
            if sig.pagina != prev.pagina or sig.columna != prev.columna:
                break
            if sig.top - prev.bottom > 2.5 * max(prev.size, 6):
                break
            c = _celdas_de_linea(sig)
            if len(c) >= 2:
                corrida.append((sig, c))
                j += 1
                continue
            # renglón de una sola celda: continuación si hay más filas tabulares después
            if j + 1 < n and len(_celdas_de_linea(lineas[j + 1])) >= 2 and lineas[j + 1].pagina == sig.pagina:
                corrida.append((sig, c))
                j += 1
                continue
            break
        multi = [x for x in corrida if len(x[1]) >= 2]
        if len(multi) < 2:
            resultado.append(l)
            i += 1
            continue

        intervalos = [(min(p.x0 for p in c), max(p.x1 for p in c)) for _, cs in multi for c in cs]
        cols = _columnas_por_solape(intervalos)
        media_celdas = statistics.mean(len(cs) for _, cs in multi)
        if len(cols) < 2 or len(cols) > max(2, round(media_celdas * 1.6)):
            resultado.append(l)
            i += 1
            continue

        def col_de(celda):
            cx0 = min(p.x0 for p in celda)
            cx1 = max(p.x1 for p in celda)
            mejor, solape_max = 0, -1e9
            for k, (a, b) in enumerate(cols):
                s = min(cx1, b) - max(cx0, a)
                if s > solape_max:
                    mejor, solape_max = k, s
            return mejor

        huecos = [corrida[k][0].top - corrida[k - 1][0].bottom for k in range(1, len(corrida))]
        corte = None
        if huecos and max(huecos) > 1.8 * max(min(huecos), 0.5) + 1:
            corte = (min(huecos) + max(huecos)) / 2

        filas: list[list[list[Palabra]]] = []
        for k, (lin, cs) in enumerate(corrida):
            fila = [[] for _ in cols]
            for c in cs:
                fila[col_de(c)].extend(c)
            continua = False
            if filas:
                hueco = lin.top - corrida[k - 1][0].bottom
                if not fila[0] or (corte is not None and hueco < corte):
                    continua = True
            if continua:
                for ci, ws in enumerate(fila):
                    filas[-1][ci].extend(ws)
            else:
                filas.append(fila)

        tabla = [[_texto_palabras(c) for c in f] for f in filas]
        if len(tabla) < 2:
            resultado.append(l)
            i += 1
            continue
        resultado.append(Bloque(tipo="tabla", pagina=l.pagina, top=l.top, filas=tabla, columna=l.columna))
        i = j
    return resultado


def _texto_palabras(ws: list[Palabra]) -> str:
    return _unir_palabras(ws, formato=False)


# ---------------------------------------------------------------------------
# Construcción de bloques
# ---------------------------------------------------------------------------

def _marcador_lista(l: Linea):
    """Devuelve (marcador_md, palabras_sin_marcador) si la línea es un elemento de lista."""
    ws = l.palabras
    primera = ws[0].texto
    if RE_VINETA.match(primera) and len(ws) > 1:
        return "-", ws[1:]
    m = RE_VINETA_PEGADA.match(primera)
    if m and len(ws) >= 1:
        resto = Palabra(m.group(2), ws[0].x0, ws[0].x1, ws[0].top, ws[0].bottom, ws[0].size, ws[0].negrita, ws[0].cursiva)
        return "-", [resto] + ws[1:]
    if RE_NUMERO.match(primera) and len(ws) > 1:
        num = primera.strip("()")
        if re.match(r"^\d{1,3}[.)]$", num):
            return num[:-1] + ".", ws[1:]
        return "- " + escapar_md(primera), ws[1:]
    return None


def _estadisticas(lineas_por_pagina: dict, factor_titulo: float = 1.15, tolerancia_rel: float = 0.0):
    """Tamaño de letra del cuerpo, niveles de título por tamaño e interlineado típico."""
    todas = [l for pg in sorted(lineas_por_pagina) for l in lineas_por_pagina[pg]]
    pesos = Counter()
    for l in todas:
        for p in l.palabras:
            pesos[round(p.size * 2) / 2] += len(p.texto)
    cuerpo = pesos.most_common(1)[0][0] if pesos else 10.0
    niveles = _niveles_titulo(todas, cuerpo, factor_titulo)
    tolerancia = max(1.0, tolerancia_rel * cuerpo)
    huecos = []
    for lineas in lineas_por_pagina.values():
        for a, b in zip(lineas, lineas[1:]):
            if a.columna == b.columna and abs(a.size - cuerpo) <= tolerancia and abs(b.size - cuerpo) <= tolerancia:
                h = b.top - a.bottom
                if 0 <= h < cuerpo * 3:
                    huecos.append(h)
    interlineado = statistics.median(huecos) if huecos else cuerpo * 0.3
    return cuerpo, niveles, interlineado


def _niveles_titulo(lineas: list[Linea], cuerpo: float, factor: float = 1.15) -> dict:
    tam = Counter()
    for l in lineas:
        s = l.size
        if s >= cuerpo * factor and len(l.texto) < 200:
            tam[round(s)] += 1
    tamanos = sorted(tam.keys(), reverse=True)
    return {t: min(i + 1, 4) for i, t in enumerate(tamanos[:6])}


def _unir_palabras(ws: list[Palabra], formato: bool = True) -> str:
    """Une palabras resolviendo guiones de corte y aplicando negrita/cursiva."""
    # unir cortes de palabra con guion al final de renglón
    tokens: list[Palabra] = []
    for w in ws:
        if tokens:
            prev = tokens[-1]
            salto_linea = w.top > prev.bottom - 1 or w.x0 < prev.x0
            if salto_linea and re.search(r"[a-záéíóúñü]-$", prev.texto) and re.match(r"^[a-záéíóúñü]", w.texto):
                tokens[-1] = Palabra(prev.texto[:-1] + w.texto, prev.x0, w.x1, prev.top, w.bottom,
                                     prev.size, prev.negrita and w.negrita, prev.cursiva and w.cursiva)
                continue
        tokens.append(w)

    # ¿lleva espacio antes cada token? (no, si está pegado a la palabra anterior)
    espacios = [False]
    for k in range(1, len(tokens)):
        a, b = tokens[k - 1], tokens[k]
        mismo_renglon = abs(a.top - b.top) < 0.5 * max(a.size, 1) and b.x0 >= a.x0
        espacios.append(not (mismo_renglon and b.x0 - a.x1 < 0.12 * max(a.size, b.size)))

    def juntar(idx):
        out = ""
        for n, k in enumerate(idx):
            out += (" " if n and espacios[k] else "") + tokens[k].texto
        return out

    if not formato:
        return juntar(range(len(tokens))).strip()

    # estilo efectivo: la puntuación hereda el estilo anterior
    estilos = []
    for k, t in enumerate(tokens):
        if not re.search(r"\w", t.texto) and estilos:
            estilos.append(estilos[-1])
        else:
            estilos.append((t.negrita, t.cursiva))
    todo_negrita = all(e[0] for e in estilos)
    todo_cursiva = all(e[1] for e in estilos)

    salida = ""
    k = 0
    while k < len(tokens):
        neg, cur = estilos[k]
        k2 = k + 1
        while k2 < len(tokens) and estilos[k2] == (neg, cur):
            k2 += 1
        texto = escapar_md(juntar(range(k, k2)))
        marca = ""
        if neg and not todo_negrita:
            marca += "**"
        if cur and not todo_cursiva:
            marca += "*"
        if marca and re.search(r"\w", texto):
            # dejar la puntuación final fuera del énfasis
            m = re.match(r"^(.*?\w.*?)([\s.,;:)]*)$", texto, re.S)
            nucleo, cola = (m.group(1), m.group(2)) if m else (texto, "")
            texto = f"{marca}{nucleo}{marca[::-1]}{cola}"
        salida += (" " if k and espacios[k] else "") + texto
        k = k2
    salida = salida.strip()
    if todo_negrita and tokens and re.search(r"\w", salida):
        salida = f"**{salida}**"
    elif todo_cursiva and tokens and re.search(r"\w", salida):
        salida = f"*{salida}*"
    return salida


def _construir_bloques(items: list, cuerpo: float, niveles: dict, interlineado: float,
                       titulos_negrita: bool = True) -> list[Bloque]:
    bloques: list[Bloque] = []
    nivel_negrita = min(max(niveles.values(), default=1) + 1, 6)
    nivel_negrita = max(nivel_negrita, 2)
    xs_lista: list[float] = []

    actual: Optional[Bloque] = None
    prev_linea: Optional[Linea] = None

    def cerrar():
        nonlocal actual
        if actual is not None:
            bloques.append(actual)
        actual = None

    for it in items:
        if isinstance(it, Bloque):
            cerrar()
            bloques.append(it)
            prev_linea = None
            continue
        l: Linea = it
        texto = l.texto.strip()
        if not texto:
            continue

        size = l.size
        nivel_tam = niveles.get(round(size))
        es_titulo_negrita = (
            titulos_negrita and nivel_tam is None and l.negrita and len(l.palabras) <= 14
            and not re.search(r"[.,;:]$", texto) and abs(size - cuerpo) < cuerpo * 0.15
            and _marcador_lista(l) is None
        )
        hueco = (l.top - prev_linea.bottom) if prev_linea and prev_linea.pagina == l.pagina else None
        cambio_columna = prev_linea is not None and (prev_linea.columna != l.columna or prev_linea.pagina != l.pagina)

        # --- títulos
        if nivel_tam is not None or es_titulo_negrita:
            nivel = nivel_tam if nivel_tam is not None else nivel_negrita
            if (actual is not None and actual.tipo == "titulo" and actual.nivel == nivel
                    and hueco is not None and hueco < size * 0.9 and not cambio_columna):
                actual.palabras.extend(l.palabras)
            else:
                cerrar()
                actual = Bloque(tipo="titulo", pagina=l.pagina, top=l.top, nivel=nivel,
                                palabras=list(l.palabras), columna=l.columna)
            prev_linea = l
            continue

        # --- listas
        lista = _marcador_lista(l)
        if lista is not None:
            cerrar()
            marcador, ws = lista
            x = l.x0
            xs_lista[:] = [v for v in xs_lista if v <= x + 3]
            if not xs_lista or x > xs_lista[-1] + 6:
                xs_lista.append(x)
            nivel = len(xs_lista) - 1
            actual = Bloque(tipo="lista", pagina=l.pagina, top=l.top, marcador=marcador,
                            palabras=list(ws), nivel=nivel, x0=ws[0].x0 if ws else x, columna=l.columna)
            prev_linea = l
            continue

        # --- continuación de lista o párrafo
        umbral_parrafo = max(interlineado * 1.6, interlineado + 0.35 * size)
        continua = False
        if actual is not None and prev_linea is not None and actual.tipo in ("lista", "parrafo"):
            if hueco is not None and not cambio_columna:
                if hueco <= umbral_parrafo:
                    if actual.tipo == "lista":
                        continua = l.x0 >= actual.x0 - 4
                    else:
                        sangria = l.x0 - prev_linea.x0
                        fin_frase = re.search(r"[.!?:]$", prev_linea.texto.strip())
                        continua = not (sangria > 8 and fin_frase)
            elif cambio_columna and actual.tipo == "parrafo":
                # párrafo que sigue en la otra columna o en la página siguiente
                ultimo = prev_linea.texto.strip()
                continua = bool(not re.search(r"[.!?:]$", ultimo) and re.match(r"^[a-záéíóúñü(]", texto))
            if abs(size - (actual.palabras[0].size if actual.palabras else size)) > 1.5:
                continua = False

        if continua:
            actual.palabras.extend(l.palabras)
        else:
            cerrar()
            xs_lista.clear()
            actual = Bloque(tipo="parrafo", pagina=l.pagina, top=l.top, palabras=list(l.palabras),
                            x0=l.x0, columna=l.columna)
        prev_linea = l
    cerrar()
    return bloques


# ---------------------------------------------------------------------------
# Render Markdown
# ---------------------------------------------------------------------------

def tabla_a_markdown(filas: list[list[str]]) -> str:
    if not filas:
        return ""
    ncol = max(len(f) for f in filas)
    filas = [[(c or "").replace("|", "\\|").replace("\n", " ").strip() for c in f] + [""] * (ncol - len(f)) for f in filas]
    filas = [[escapar_md(c).replace("\\\\|", "\\|") for c in f] for f in filas]
    anchos = [max(3, max(len(f[c]) for f in filas)) for c in range(ncol)]
    anchos = [min(a, 60) for a in anchos]

    def linea(f):
        return "| " + " | ".join(f[c].ljust(anchos[c]) for c in range(ncol)) + " |"

    out = [linea(filas[0]), "| " + " | ".join("-" * anchos[c] for c in range(ncol)) + " |"]
    out += [linea(f) for f in filas[1:]]
    return "\n".join(out)


def _render(bloques: list[Bloque]) -> str:
    partes: list[str] = []
    prev_tipo = None
    for b in bloques:
        if b.tipo == "titulo":
            texto = _unir_palabras(b.palabras, formato=False)
            texto = escapar_md(texto).strip()
            if not texto:
                continue
            partes.append(("\n" if partes else "") + "#" * b.nivel + " " + texto + "\n")
        elif b.tipo == "parrafo":
            texto = escapar_inicio_linea(_unir_palabras(b.palabras))
            partes.append(texto + "\n")
        elif b.tipo == "lista":
            texto = _unir_palabras(b.palabras) if b.palabras else ""
            sangria = "    " * b.nivel
            linea = f"{sangria}{b.marcador} {texto}".rstrip()
            if prev_tipo == "lista":
                partes[-1] = partes[-1].rstrip("\n") + "\n"
                partes.append(linea + "\n")
            else:
                partes.append(linea + "\n")
        elif b.tipo == "tabla":
            partes.append(tabla_a_markdown(b.filas) + "\n")
        elif b.tipo == "imagen":
            partes.append(b.texto + "\n")
        elif b.tipo == "nota":
            partes.append(b.texto + "\n")
        prev_tipo = b.tipo

    md = ""
    for k, p in enumerate(partes):
        if k == 0:
            md = p
            continue
        # listas consecutivas sin línea en blanco; resto separado por una línea vacía
        if bloques_tipo_lista(p) and bloques_tipo_lista(partes[k - 1]):
            md += p
        else:
            md += "\n" + p
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def bloques_tipo_lista(texto: str) -> bool:
    return bool(re.match(r"^\s*(- |\d{1,3}\. )", texto))


def _frontmatter(titulo: str, origen: str, paginas: int, etiquetas: list[str]) -> str:
    t = titulo.replace('"', "'")
    o = origen.replace('"', "'")
    lineas = [
        "---",
        f'title: "{t}"',
        f'source: "{o}"',
        f"created: {date.today().isoformat()}",
        f"pages: {paginas}",
        "tags:",
    ]
    lineas += [f"  - {e}" for e in etiquetas]
    lineas.append("---")
    return "\n".join(lineas) + "\n\n"


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def convertir_pdf(
    ruta_pdf: str,
    carpeta_imagenes: Optional[str] = None,
    prefijo_imagenes: Optional[str] = None,
    incluir_frontmatter: bool = True,
    etiquetas: Optional[list[str]] = None,
    nombre_original: Optional[str] = None,
    motor_ocr=None,
) -> ResultadoConversion:
    """Convierte un PDF a Markdown listo para Obsidian.

    carpeta_imagenes: si se indica, las imágenes del PDF se guardan ahí y se
                      enlazan como ![[nombre.png]] (formato Obsidian).
    motor_ocr:        motor de ocr_motores.crear_motor(). Si se indica, se lee el
                      texto de las imágenes y de las páginas escaneadas.
    """
    nombre_original = nombre_original or os.path.basename(ruta_pdf)
    base = os.path.splitext(nombre_original)[0]
    prefijo = prefijo_imagenes or re.sub(r"[^\w\-]+", "-", base).strip("-")[:40] or "img"

    lineas_por_pagina: dict[int, list[Linea]] = {}
    tablas_por_pagina: dict[int, list[Bloque]] = defaultdict(list)
    imagenes_por_pagina: dict[int, list[Bloque]] = defaultdict(list)
    alturas: dict[int, float] = {}
    sin_texto: list[int] = []
    rutas_imagenes: list[str] = []
    titulo_meta = ""
    contador_img = 0
    imagenes_ocr = 0
    paginas_ocr: list[int] = []

    with pdfplumber.open(ruta_pdf) as pdf:
        titulo_meta = (pdf.metadata or {}).get("Title", "") or ""
        if isinstance(titulo_meta, bytes):
            titulo_meta = titulo_meta.decode("utf-8", "ignore")
        total = len(pdf.pages)
        for idx, page in enumerate(pdf.pages, start=1):
            alturas[idx] = page.height
            tablas = _tablas_con_bordes(page)
            for bbox, filas in tablas:
                tablas_por_pagina[idx].append(Bloque(tipo="tabla", pagina=idx, top=bbox[1], filas=filas, x0=bbox[0]))
            palabras = _palabras_pagina(page, [b for b, _ in tablas])
            lineas_por_pagina[idx] = _lineas_pagina(palabras, idx, page.width)

            escaneada = not palabras and not tablas
            imgs = _procesar_imagenes(page, idx, carpeta_imagenes, prefijo, contador_img, palabras,
                                      motor_ocr=None if escaneada else motor_ocr)
            contador_img += sum(1 for i in imgs if i[2])
            embeds = []
            for top, x0, nombre, ruta, md_ocr in imgs:
                if ruta:
                    rutas_imagenes.append(ruta)
                    embeds.append(f"![[{nombre}]]")
                if escaneada:
                    continue
                if nombre:
                    imagenes_por_pagina[idx].append(
                        Bloque(tipo="imagen", pagina=idx, top=top, x0=x0, texto=f"![[{nombre}]]"))
                if md_ocr:
                    imagenes_ocr += 1
                    imagenes_por_pagina[idx].append(Bloque(
                        tipo="nota", pagina=idx, top=top, x0=x0,
                        texto=callout("quote", "📷 Texto extraído de una imagen del PDF (OCR)", md_ocr),
                    ))

            if escaneada:
                sin_texto.append(idx)
                md_pagina = ""
                if motor_ocr is not None:
                    try:
                        md_pagina = ocr_a_markdown(page.to_image(resolution=300).original, motor_ocr)
                    except Exception:
                        md_pagina = ""
                if md_pagina:
                    paginas_ocr.append(idx)
                    texto_nota = callout(
                        "info", f"📄 Página {idx} escaneada · texto obtenido con OCR (revisa posibles errores)",
                        "\n".join(embeds), plegado=True,
                    ) + "\n\n" + md_pagina
                elif motor_ocr is not None:
                    texto_nota = callout("warning", f"Página {idx}: imagen sin texto reconocible", "\n".join(embeds))
                else:
                    texto_nota = callout(
                        "warning", f"Página {idx} sin texto seleccionable",
                        "Parece una página escaneada: activa el OCR para extraer su texto."
                        + ("\n\n" + "\n".join(embeds) if embeds else ""),
                    )
                imagenes_por_pagina[idx].append(Bloque(tipo="nota", pagina=idx, top=0, texto=texto_nota))

    _quitar_encabezados_pies(lineas_por_pagina, alturas)

    cuerpo, niveles, interlineado = _estadisticas(lineas_por_pagina)

    # secuencia final: líneas + tablas alineadas + tablas con bordes + imágenes + notas
    items: list = []
    for pg in sorted(lineas_por_pagina):
        secuencia = _detectar_tablas_alineadas(lineas_por_pagina[pg])
        extras = sorted(tablas_por_pagina[pg] + imagenes_por_pagina[pg], key=lambda b: b.top)
        for extra in extras:
            pos = len(secuencia)
            for k, s in enumerate(secuencia):
                if s.top > extra.top:
                    pos = k
                    break
            secuencia.insert(pos, extra)
        items.extend(secuencia)

    # unir tablas partidas entre páginas (misma cantidad de columnas, consecutivas)
    fusionados: list = []
    for it in items:
        if (isinstance(it, Bloque) and it.tipo == "tabla" and fusionados
                and isinstance(fusionados[-1], Bloque) and fusionados[-1].tipo == "tabla"
                and fusionados[-1].pagina == it.pagina - 1
                and len(fusionados[-1].filas[0]) == len(it.filas[0])):
            filas = it.filas[1:] if it.filas[0] == fusionados[-1].filas[0] else it.filas
            fusionados[-1].filas.extend(filas)
            continue
        fusionados.append(it)

    bloques = _construir_bloques(fusionados, cuerpo, niveles, interlineado)
    cuerpo_md = _render(bloques)

    titulo = titulo_meta.strip()
    if not titulo or len(titulo) < 3 or titulo.lower().startswith(("microsoft word", "untitled", "documento", "(anonymous)", "anonymous")):
        m = re.search(r"^#{1,2} (.+)$", cuerpo_md, re.M)
        titulo = m.group(1).replace("\\", "").strip() if m else base

    md = cuerpo_md
    if incluir_frontmatter:
        md = _frontmatter(titulo, nombre_original, total, etiquetas or ["pdf"]) + cuerpo_md

    return ResultadoConversion(
        markdown=md, titulo=titulo, paginas=total, imagenes=rutas_imagenes, paginas_sin_texto=sin_texto,
        imagenes_ocr=imagenes_ocr, paginas_ocr=paginas_ocr,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python pdf_obsidian.py archivo.pdf [carpeta_imagenes|-] [ocr: auto|windows|tesseract|no]")
        sys.exit(1)
    sys.stdout.reconfigure(encoding="utf-8")
    motor = None
    if len(sys.argv) > 3:
        from ocr_motores import crear_motor
        motor, msg = crear_motor(sys.argv[3])
        print(msg, file=sys.stderr)
    carpeta = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    r = convertir_pdf(sys.argv[1], carpeta, motor_ocr=motor)
    print(r.markdown)
