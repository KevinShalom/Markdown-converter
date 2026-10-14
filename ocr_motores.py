"""
ocr_motores.py
--------------
Motores de OCR para leer texto dentro de imágenes de un PDF.

  * "windows"   -> OCR nativo de Windows 10/11 (gratis, sin instalar programas).
                   Requiere:  py -m pip install winocr
                   y el idioma (p. ej. Español) instalado en Windows.
  * "tesseract" -> Tesseract OCR (gratis). Requiere instalar Tesseract para Windows
                   (https://github.com/UB-Mannheim/tesseract/wiki) con el idioma
                   "Spanish", y:  py -m pip install pytesseract
  * "llm"       -> Un modelo de IA con visión, mediante una API compatible con OpenAI
                   (de pago, mejor con tablas y diseños complejos).
                   Requiere:  py -m pip install openai
  * "auto"      -> Usa Windows si está disponible; si no, Tesseract.

Los motores "windows" y "tesseract" devuelven palabras con su posición, y
pdf_obsidian.py les aplica el mismo análisis de estructura que al texto normal
(títulos, listas, tablas). El motor "llm" devuelve Markdown directamente.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil


def _get(obj, clave):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(clave)
    return getattr(obj, clave, None)


class MotorOCR:
    nombre = "base"
    devuelve_markdown = False

    def lineas(self, img):
        """Devuelve [[{text, x0, top, x1, bottom}, ...], ...] en píxeles, una lista por renglón."""
        raise NotImplementedError

    def markdown(self, img) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Windows (Windows.Media.Ocr)
# ---------------------------------------------------------------------------

class MotorWindows(MotorOCR):
    nombre = "windows"

    def __init__(self, idioma: str = "es"):
        import winocr  # noqa: F401
        from PIL import Image

        self.winocr = winocr
        candidatos = []
        for c in (idioma, "es-MX", "es-ES", "es", "en-US", "en"):
            if c and c not in candidatos:
                candidatos.append(c)
        prueba = Image.new("RGB", (64, 32), "white")
        self.idioma = None
        errores = []
        for c in candidatos:
            try:
                self._reconocer(prueba, c)
                self.idioma = c
                break
            except Exception as e:  # idioma no instalado en Windows
                errores.append(f"{c}: {e}")
        if self.idioma is None:
            raise RuntimeError("El OCR de Windows no tiene ningún idioma disponible. " + "; ".join(errores))

    def _reconocer(self, img, idioma):
        img = img.convert("RGBA")
        if hasattr(self.winocr, "recognize_pil_sync"):
            return self.winocr.recognize_pil_sync(img, idioma)
        import asyncio

        res = asyncio.run(self.winocr.recognize_pil(img, idioma))
        if hasattr(self.winocr, "picklify"):
            res = self.winocr.picklify(res)
        return res

    def lineas(self, img):
        # Windows limita el tamaño de imagen; reducimos si hace falta
        escala = 1.0
        lim = 4000
        if max(img.size) > lim:
            escala = lim / max(img.size)
            img = img.resize((int(img.width * escala), int(img.height * escala)))
        res = self._reconocer(img, self.idioma)
        salida = []
        for ln in _get(res, "lines") or []:
            palabras = []
            for w in _get(ln, "words") or []:
                r = _get(w, "bounding_rect")
                x, y, an, al = (_get(r, "x"), _get(r, "y"), _get(r, "width"), _get(r, "height"))
                texto = _get(w, "text") or ""
                if None in (x, y, an, al) or not texto.strip():
                    continue
                palabras.append({
                    "text": texto, "x0": x / escala, "top": y / escala,
                    "x1": (x + an) / escala, "bottom": (y + al) / escala,
                })
            if palabras:
                salida.append(palabras)
        return salida


# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------

RUTAS_TESSERACT_WINDOWS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

IDIOMAS_TESSERACT = {"es": "spa", "en": "eng", "pt": "por", "fr": "fra", "de": "deu", "it": "ita"}


class MotorTesseract(MotorOCR):
    nombre = "tesseract"

    def __init__(self, idioma: str = "es"):
        import pytesseract

        self.pt = pytesseract
        exe = shutil.which("tesseract")
        if not exe:
            exe = next((r for r in RUTAS_TESSERACT_WINDOWS if os.path.isfile(r)), None)
        if not exe:
            raise RuntimeError("No encuentro tesseract.exe. Instala Tesseract OCR para Windows.")
        pytesseract.pytesseract.tesseract_cmd = exe
        disponibles = set(pytesseract.get_languages(config=""))
        principal = IDIOMAS_TESSERACT.get((idioma or "es").split("-")[0].lower(), "spa")
        elegidos = [l for l in (principal, "eng") if l in disponibles]
        if not elegidos:
            elegidos = [sorted(disponibles - {"osd"})[0]] if disponibles - {"osd"} else ["eng"]
        self.idioma = "+".join(dict.fromkeys(elegidos))
        self.falta_idioma = principal not in disponibles

    def lineas(self, img):
        datos = self.pt.image_to_data(
            img.convert("RGB"), lang=self.idioma, config="--psm 3", output_type=self.pt.Output.DICT
        )
        grupos: dict = {}
        for i, texto in enumerate(datos["text"]):
            texto = (texto or "").strip()
            try:
                conf = float(datos["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if not texto or conf < 45:
                continue
            clave = (datos["block_num"][i], datos["par_num"][i], datos["line_num"][i])
            x, y, an, al = datos["left"][i], datos["top"][i], datos["width"][i], datos["height"][i]
            grupos.setdefault(clave, []).append(
                {"text": texto, "x0": x, "top": y, "x1": x + an, "bottom": y + al}
            )
        return list(grupos.values())


# ---------------------------------------------------------------------------
# Modelo de IA con visión (API compatible con OpenAI)
# ---------------------------------------------------------------------------

PROMPT_LLM = (
    "Transcribe TODO el texto visible en esta imagen y devuélvelo en Markdown, respetando "
    "su estructura: títulos (##, ###), listas, negritas y tablas Markdown cuando el texto "
    "esté en forma de tabla. No describas la imagen, no traduzcas, no agregues comentarios "
    "ni bloques de código. Si la imagen no contiene texto legible responde exactamente: SIN_TEXTO"
)


class MotorLLM(MotorOCR):
    nombre = "llm"
    devuelve_markdown = True

    def __init__(self, modelo: str, api_key: str = "", base_url: str = ""):
        from openai import OpenAI

        api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("Falta la clave de API para el OCR con IA.")
        self.cliente = OpenAI(api_key=api_key, base_url=base_url or None)
        self.modelo = modelo or "gpt-4o-mini"

    def markdown(self, img) -> str:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        r = self.cliente.chat.completions.create(
            model=self.modelo,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": PROMPT_LLM},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            temperature=0,
        )
        texto = (r.choices[0].message.content or "").strip()
        texto = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", texto)
        return "" if texto.strip() == "SIN_TEXTO" else texto.strip()


# ---------------------------------------------------------------------------
# Selección
# ---------------------------------------------------------------------------

def crear_motor(nombre: str = "auto", idioma: str = "es", llm_modelo: str = "", llm_api_key: str = "",
                llm_url: str = ""):
    """Devuelve (motor | None, mensaje). Nunca lanza excepción."""
    nombre = (nombre or "auto").lower()
    if nombre in ("no", "off", "ninguno", ""):
        return None, "OCR desactivado"
    intentos = {"auto": ["windows", "tesseract"], "windows": ["windows"], "tesseract": ["tesseract"], "llm": ["llm"]}
    errores = []
    for n in intentos.get(nombre, ["windows", "tesseract"]):
        try:
            if n == "windows":
                m = MotorWindows(idioma)
                return m, f"OCR de Windows ({m.idioma})"
            if n == "tesseract":
                m = MotorTesseract(idioma)
                aviso = " — falta el idioma español en Tesseract" if m.falta_idioma else ""
                return m, f"Tesseract ({m.idioma}){aviso}"
            if n == "llm":
                m = MotorLLM(llm_modelo, llm_api_key, llm_url)
                return m, f"IA ({m.modelo})"
        except ImportError as e:
            errores.append(f"{n}: falta instalar el paquete ({e.name})")
        except Exception as e:
            errores.append(f"{n}: {e}")
    return None, "OCR no disponible — " + " | ".join(errores)
