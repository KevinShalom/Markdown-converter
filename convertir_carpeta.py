"""
Convierte todos los PDF de una carpeta (y subcarpetas) a notas de Obsidian.

Uso:
    py -3.14 convertir_carpeta.py "C:\\ruta\\a\\mis PDFs"
    py -3.14 convertir_carpeta.py "C:\\ruta\\a\\mis PDFs" "C:\\MiBoveda\\PDFs importados"
    py -3.14 convertir_carpeta.py "C:\\ruta\\a\\mis PDFs" --forzar     (reconvierte aunque ya exista la nota)
    py -3.14 convertir_carpeta.py "C:\\ruta\\a\\mis PDFs" --ocr no     (sin OCR; por defecto usa el de la app)

Si no indicas destino, usa la bóveda configurada en la app web (config_obsidian.json).
"""
import argparse
import json
import os
import sys

from ocr_motores import crear_motor
from pdf_obsidian import convertir_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def config_app():
    try:
        with open(os.path.join(BASE_DIR, "config_obsidian.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


BOVEDA_DEFECTO = r"C:\Users\PC\Desktop\KPI-Holding"


def destino_configurado():
    c = config_app()
    boveda = c.get("boveda") or BOVEDA_DEFECTO
    return os.path.join(boveda, c.get("subcarpeta", "PDFs importados")), c.get("adjuntos", "adjuntos")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="PDF -> notas de Obsidian")
    ap.add_argument("origen", help="Carpeta con PDFs (o un solo PDF)")
    ap.add_argument("destino", nargs="?", help="Carpeta dentro de tu bóveda donde guardar las notas")
    ap.add_argument("--adjuntos", default=None, help="Subcarpeta para imágenes (por defecto 'adjuntos')")
    ap.add_argument("--sin-imagenes", action="store_true")
    ap.add_argument("--forzar", action="store_true", help="Sobrescribe notas existentes")
    ap.add_argument("--ocr", default=None, help="auto | windows | tesseract | llm | no (por defecto, el de la app)")
    a = ap.parse_args()

    c = config_app()
    motor, msg = crear_motor(a.ocr or c.get("ocr", "auto"), c.get("ocr_idioma", "es"),
                             c.get("llm_modelo", ""), c.get("llm_api_key", ""), c.get("llm_url", ""))
    print(f"OCR: {msg}")

    destino, adj = (a.destino, a.adjuntos or "adjuntos") if a.destino else destino_configurado()
    if a.adjuntos:
        adj = a.adjuntos
    if not destino:
        print("Indica la carpeta destino o configura la bóveda en la app web primero.")
        sys.exit(1)
    os.makedirs(destino, exist_ok=True)

    if os.path.isfile(a.origen):
        pdfs = [a.origen]
        raiz = os.path.dirname(a.origen)
    else:
        raiz = a.origen
        pdfs = [os.path.join(d, f) for d, _, fs in os.walk(a.origen) for f in fs if f.lower().endswith(".pdf")]
    pdfs.sort()
    print(f"{len(pdfs)} PDF encontrados. Destino: {destino}\n")

    ok = err = saltados = 0
    for ruta in pdfs:
        rel = os.path.relpath(os.path.dirname(ruta), raiz)
        carpeta_nota = os.path.normpath(os.path.join(destino, rel)) if rel != "." else destino
        os.makedirs(carpeta_nota, exist_ok=True)
        nombre = os.path.splitext(os.path.basename(ruta))[0]
        ruta_md = os.path.join(carpeta_nota, nombre + ".md")
        if os.path.exists(ruta_md) and not a.forzar:
            saltados += 1
            print(f"  ↷ ya existe: {os.path.relpath(ruta_md, destino)}")
            continue
        try:
            res = convertir_pdf(ruta, carpeta_imagenes=None if a.sin_imagenes else os.path.join(carpeta_nota, adj),
                                motor_ocr=motor)
            with open(ruta_md, "w", encoding="utf-8", newline="\n") as f:
                f.write(res.markdown)
            aviso = ""
            if res.imagenes_ocr or res.paginas_ocr:
                aviso += f"  🔎 OCR: {res.imagenes_ocr} img, {len(res.paginas_ocr)} pág."
            faltan = len(res.paginas_sin_texto) - len(res.paginas_ocr)
            if faltan:
                aviso += f"  ⚠ {faltan} pág. escaneadas sin texto"
            print(f"  ✓ {os.path.relpath(ruta_md, destino)}  ({res.paginas} pág., {len(res.imagenes)} img){aviso}")
            ok += 1
        except Exception as e:
            err += 1
            print(f"  ✗ {ruta}: {e}")
    print(f"\nListo: {ok} convertidos, {saltados} ya existían, {err} con error.")


if __name__ == "__main__":
    main()
