# PDF a Markdown → Obsidian

Convierte PDFs (y DOCX, PPTX, XLSX, HTML…) a Markdown bien estructurado y los guarda directamente en una bóveda de Obsidian.

Basado en [Microsoft MarkItDown](https://github.com/microsoft/markitdown) (licencia MIT; ver `README-markitdown.md` y `LICENSE`).

## Qué hace

- **Títulos** (`#`, `##`, `###`) deducidos por tamaño de letra y negritas.
- **Párrafos** reconstruidos: une renglones cortados y palabras partidas con guion.
- **Listas** con viñetas y numeradas (anidadas), **negritas** y *cursivas*.
- **Tablas** con bordes y sin bordes (columnas alineadas); une tablas partidas entre páginas.
- **Documentos a dos columnas** en orden de lectura correcto.
- Quita **encabezados, pies y números de página** repetidos.
- **Imágenes** extraídas a `adjuntos/` y enlazadas como `![[imagen.png]]`.
- **OCR**: lee el texto dentro de imágenes y páginas escaneadas, con formato, marcado en un callout `📷 Texto extraído de una imagen del PDF (OCR)`.
- Escapa caracteres que Obsidian malinterpreta (`$`, `#etiqueta`, `==`, `[[`…).
- Añade **propiedades** (frontmatter): título, origen, fecha, etiqueta `pdf`.

## Uso

1. Doble clic en **`iniciar.bat`** (instala dependencias y abre `http://localhost:8000`).
2. Arrastra uno o varios PDF → **Convertir** → **Guardar en Obsidian** (o **Guardar todo**).
3. En ⚙️ puedes cambiar la bóveda, carpetas y el motor de OCR.

La bóveda por defecto está en `CONFIG_DEFECTO` dentro de `app.py`.

### Convertir una carpeta completa

```
py -3.14 convertir_carpeta.py "C:\ruta\de\tus PDFs"
py -3.14 convertir_carpeta.py "C:\ruta\de\tus PDFs" "C:\MiBoveda\PDFs" --ocr no --forzar
```

## Motores de OCR (`ocr_motores.py`)

| Motor | Costo | Requisitos |
| --- | --- | --- |
| `windows` | Gratis | `pip install winocr` + idioma español instalado en Windows |
| `tesseract` | Gratis | [Tesseract para Windows](https://github.com/UB-Mannheim/tesseract/wiki) con idioma Spanish + `pip install pytesseract` |
| `llm` | De pago | `pip install openai` + clave de API (cualquier API compatible con OpenAI) |
| `auto` | — | Windows y, si no está, Tesseract |

## Archivos

| Archivo | Función |
| --- | --- |
| `app.py` | Servidor web local (FastAPI) e interfaz |
| `pdf_obsidian.py` | Motor de conversión PDF → Markdown |
| `ocr_motores.py` | Motores de OCR |
| `convertir_carpeta.py` | Conversión por lotes desde la terminal |
| `iniciar.bat` | Lanzador para Windows |
| `requirements-obsidian.txt` | Dependencias |
