"""
MarkItDown -> Obsidian
Servidor web local para convertir PDF (y otros documentos) a Markdown
y guardarlos directamente en tu bóveda de Obsidian.

Ejecuta:  py -3.14 app.py      (o usa iniciar.bat)
Abre:     http://localhost:8000
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import date

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ocr_motores import crear_motor
from pdf_obsidian import convertir_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config_obsidian.json")
TRABAJOS_DIR = os.path.join(tempfile.gettempdir(), "markitdown_obsidian")
os.makedirs(TRABAJOS_DIR, exist_ok=True)

CONFIG_DEFECTO = {
    "boveda": r"C:\Users\PC\Desktop\KPI-Holding",  # bóveda de Obsidian por defecto
    "subcarpeta": "PDFs importados",  # carpeta dentro de la bóveda para las notas
    "adjuntos": "adjuntos",           # carpeta (dentro de la subcarpeta) para imágenes
    "frontmatter": True,
    "imagenes": True,
    "ocr": "auto",                    # auto | windows | tesseract | llm | no
    "ocr_idioma": "es",
    "llm_url": "",                    # vacío = OpenAI; o cualquier API compatible
    "llm_modelo": "gpt-4o-mini",
    "llm_api_key": "",
}

_motor_cache = {"clave": None, "motor": None, "mensaje": ""}


def motor_ocr(cfg: dict):
    """Crea (y reutiliza) el motor OCR según la configuración."""
    clave = (cfg.get("ocr"), cfg.get("ocr_idioma"), cfg.get("llm_url"), cfg.get("llm_modelo"), cfg.get("llm_api_key"))
    if _motor_cache["clave"] != clave:
        m, msg = crear_motor(cfg.get("ocr", "auto"), cfg.get("ocr_idioma", "es"),
                             cfg.get("llm_modelo", ""), cfg.get("llm_api_key", ""), cfg.get("llm_url", ""))
        _motor_cache.update(clave=clave, motor=m, mensaje=msg)
    return _motor_cache["motor"], _motor_cache["mensaje"]

app = FastAPI(title="MarkItDown → Obsidian")

_md = None


def markitdown():
    """MarkItDown se carga sólo si hace falta (para DOCX, PPTX, XLSX, etc.)."""
    global _md
    if _md is None:
        from markitdown import MarkItDown
        _md = MarkItDown()
    return _md


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def leer_config() -> dict:
    cfg = dict(CONFIG_DEFECTO)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg.get("boveda"):
        cfg["boveda"] = CONFIG_DEFECTO["boveda"]
    return cfg


def guardar_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def nombre_seguro(nombre: str) -> str:
    """Nombre de archivo válido en Windows y cómodo para Obsidian."""
    nombre = re.sub(r'[<>:"/\\|?*#^\[\]\x00-\x1f]', " ", nombre)
    nombre = re.sub(r"\s+", " ", nombre).strip(" .")
    return nombre[:120] or "documento"


def limpiar_trabajos_viejos(horas: int = 24) -> None:
    limite = time.time() - horas * 3600
    for d in os.listdir(TRABAJOS_DIR):
        ruta = os.path.join(TRABAJOS_DIR, d)
        try:
            if os.path.getmtime(ruta) < limite:
                shutil.rmtree(ruta, ignore_errors=True)
        except OSError:
            pass


limpiar_trabajos_viejos()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ConfigIn(BaseModel):
    boveda: str = ""
    subcarpeta: str = "PDFs importados"
    adjuntos: str = "adjuntos"
    frontmatter: bool = True
    imagenes: bool = True
    ocr: str = "auto"
    ocr_idioma: str = "es"
    llm_url: str = ""
    llm_modelo: str = "gpt-4o-mini"
    llm_api_key: str = ""


class GuardarIn(BaseModel):
    id: str
    nombre: str
    markdown: str
    sobrescribir: bool = False


@app.get("/api/config")
def api_leer_config():
    cfg = leer_config()
    cfg["boveda_valida"] = bool(cfg["boveda"]) and os.path.isdir(cfg["boveda"])
    cfg["es_boveda_obsidian"] = cfg["boveda_valida"] and os.path.isdir(os.path.join(cfg["boveda"], ".obsidian"))
    motor, cfg["ocr_estado"] = motor_ocr(cfg)
    cfg["ocr_activo"] = motor is not None
    cfg["llm_api_key"] = "••••••" if cfg.get("llm_api_key") else ""  # no se devuelve la clave
    return cfg


@app.post("/api/config")
def api_guardar_config(c: ConfigIn):
    cfg = c.model_dump() if hasattr(c, "model_dump") else c.dict()
    cfg["boveda"] = cfg["boveda"].strip().strip('"')
    if cfg["boveda"] and not os.path.isdir(cfg["boveda"]):
        raise HTTPException(400, f"La carpeta no existe: {cfg['boveda']}")
    if cfg.get("llm_api_key") == "••••••":  # sin cambios: conservar la clave guardada
        cfg["llm_api_key"] = leer_config().get("llm_api_key", "")
    guardar_config(cfg)
    return api_leer_config()


@app.post("/api/convert")
async def api_convertir(file: UploadFile = File(...)):
    cfg = leer_config()
    nombre_original = file.filename or "documento"
    base = nombre_seguro(os.path.splitext(nombre_original)[0])
    ext = os.path.splitext(nombre_original)[1].lower()

    trabajo_id = uuid.uuid4().hex
    carpeta_trabajo = os.path.join(TRABAJOS_DIR, trabajo_id)
    carpeta_img = os.path.join(carpeta_trabajo, "img")
    os.makedirs(carpeta_trabajo)
    ruta_tmp = os.path.join(carpeta_trabajo, "entrada" + ext)
    with open(ruta_tmp, "wb") as f:
        f.write(await file.read())

    avisos = []
    try:
        if ext == ".pdf":
            motor, msg_ocr = motor_ocr(cfg)
            res = convertir_pdf(
                ruta_tmp,
                carpeta_imagenes=carpeta_img if cfg.get("imagenes", True) else None,
                incluir_frontmatter=cfg.get("frontmatter", True),
                nombre_original=nombre_original,
                motor_ocr=motor,
            )
            markdown = res.markdown
            imagenes = [os.path.basename(p) for p in res.imagenes]
            if res.imagenes_ocr:
                avisos.append(f"🔎 Texto leído con OCR en {res.imagenes_ocr} imagen(es)")
            if res.paginas_ocr:
                avisos.append(f"🔎 {len(res.paginas_ocr)} página(s) escaneada(s) leídas con OCR")
            faltan = [p for p in res.paginas_sin_texto if p not in res.paginas_ocr]
            if faltan:
                extra = "" if motor else f" ({msg_ocr})"
                avisos.append(f"⚠️ {len(faltan)} página(s) escaneada(s) sin texto extraído{extra}")
        else:
            texto = markitdown().convert(ruta_tmp).text_content
            markdown = texto
            if cfg.get("frontmatter", True):
                markdown = (
                    f'---\ntitle: "{base}"\nsource: "{nombre_original}"\n'
                    f"created: {date.today().isoformat()}\ntags:\n  - {ext.strip('.') or 'documento'}\n---\n\n"
                    + texto
                )
            imagenes = []
    except Exception as e:
        shutil.rmtree(carpeta_trabajo, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"No se pudo convertir: {e}")
    finally:
        try:
            os.remove(ruta_tmp)
        except OSError:
            pass

    return JSONResponse({
        "id": trabajo_id,
        "archivo": nombre_original,
        "nombre": base,
        "markdown": markdown,
        "imagenes": imagenes,
        "avisos": avisos,
    })


@app.post("/api/save")
def api_guardar(g: GuardarIn):
    cfg = leer_config()
    boveda = cfg.get("boveda", "")
    if not boveda or not os.path.isdir(boveda):
        raise HTTPException(400, "Primero configura la carpeta de tu bóveda de Obsidian (⚙️ arriba).")
    if not re.fullmatch(r"[0-9a-f]{32}", g.id):
        raise HTTPException(400, "Identificador de conversión no válido.")

    destino = os.path.join(boveda, cfg.get("subcarpeta", "") or "")
    os.makedirs(destino, exist_ok=True)

    nombre = nombre_seguro(g.nombre)
    ruta_md = os.path.join(destino, nombre + ".md")
    if os.path.exists(ruta_md) and not g.sobrescribir:
        n = 2
        while os.path.exists(os.path.join(destino, f"{nombre} ({n}).md")):
            n += 1
        nombre = f"{nombre} ({n})"
        ruta_md = os.path.join(destino, nombre + ".md")

    # copiar imágenes que la nota realmente usa
    carpeta_img = os.path.join(TRABAJOS_DIR, g.id, "img")
    copiadas = 0
    if os.path.isdir(carpeta_img):
        destino_img = os.path.join(destino, cfg.get("adjuntos", "adjuntos") or "adjuntos")
        for img in os.listdir(carpeta_img):
            if f"![[{img}]]" in g.markdown:
                os.makedirs(destino_img, exist_ok=True)
                shutil.copy2(os.path.join(carpeta_img, img), os.path.join(destino_img, img))
                copiadas += 1

    with open(ruta_md, "w", encoding="utf-8", newline="\n") as f:
        f.write(g.markdown)

    rel = os.path.relpath(ruta_md, boveda).replace("\\", "/")
    from urllib.parse import quote
    uri = f"obsidian://open?path={quote(os.path.abspath(ruta_md), safe='')}"
    return {"ruta": ruta_md, "relativa": rel, "imagenes": copiadas, "obsidian_uri": uri}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF → Obsidian</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
:root{
  --bg:#0a0e17; --card:rgba(18,26,43,.78); --borde:rgba(255,255,255,.08);
  --cyan:#00d2ff; --morado:#7c3aed; --grad:linear-gradient(135deg,#00d2ff 0%,#7c3aed 100%);
  --texto:#f0f4fc; --tenue:#8b9bb4; --code:#070a10; --ok:#34d399; --warn:#fbbf24; --err:#f87171;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Outfit',system-ui,sans-serif;background:var(--bg);color:var(--texto);min-height:100vh;
  display:flex;flex-direction:column;align-items:center;padding:2rem 1rem;overflow-x:hidden}
body::before{content:'';position:fixed;top:-150px;left:50%;transform:translateX(-50%);width:640px;height:360px;
  background:radial-gradient(circle,rgba(0,210,255,.15) 0%,rgba(124,58,237,.12) 50%,transparent 80%);filter:blur(60px);pointer-events:none}
.contenedor{width:100%;max-width:1000px;display:flex;flex-direction:column;gap:1.4rem;z-index:1}
header{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
h1{font-size:2rem;font-weight:700;background:linear-gradient(135deg,#fff 30%,#9cb3d8 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--tenue);font-size:.95rem}
.card{background:var(--card);backdrop-filter:blur(16px);border:1px solid var(--borde);border-radius:18px;padding:1.5rem;box-shadow:0 20px 40px rgba(0,0,0,.4)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:.7rem 1.2rem;font:600 .92rem 'Outfit',sans-serif;
  border-radius:11px;cursor:pointer;border:none;transition:all .2s;color:var(--texto);text-decoration:none;white-space:nowrap}
.btn-pri{background:var(--grad);color:#fff;box-shadow:0 4px 20px rgba(0,210,255,.25)}
.btn-pri:hover:not(:disabled){opacity:.92;transform:translateY(-1px)}
.btn-sec{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12)}
.btn-sec:hover:not(:disabled){background:rgba(255,255,255,.14)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-sm{padding:.45rem .8rem;font-size:.82rem}
/* configuración */
#panelConfig{display:none}
#panelConfig.abierto{display:block}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
label{display:flex;flex-direction:column;gap:.35rem;font-size:.85rem;color:var(--tenue)}
input[type=text]{background:var(--code);border:1px solid var(--borde);border-radius:9px;padding:.65rem .8rem;color:var(--texto);font:400 .9rem 'JetBrains Mono',monospace}
input[type=text]:focus,select:focus{outline:none;border-color:var(--cyan)}
select{background:var(--code);border:1px solid var(--borde);border-radius:9px;padding:.65rem .8rem;color:var(--texto);font:400 .9rem 'Outfit',sans-serif}
.llm{display:none} .modo-llm .llm{display:flex}
.check{flex-direction:row;align-items:center;gap:.5rem;color:var(--texto)}
.estado{font-size:.85rem;margin-top:.8rem}
.ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)}
/* zona de carga */
.drop{border:2px dashed rgba(255,255,255,.16);border-radius:14px;padding:2.4rem 1rem;text-align:center;cursor:pointer;
  background:rgba(255,255,255,.015);transition:all .25s;display:flex;flex-direction:column;align-items:center;gap:.6rem}
.drop:hover,.drop.sobre{border-color:var(--cyan);background:rgba(0,210,255,.04)}
.drop-icono{font-size:2.2rem}
.drop-t{font-size:1.1rem;font-weight:500}
.drop-s{font-size:.85rem;color:var(--tenue)}
input[type=file]{display:none}
/* lista de archivos */
#lista{display:flex;flex-direction:column;gap:.5rem;margin-top:1rem}
.fila{display:flex;align-items:center;gap:.8rem;background:rgba(255,255,255,.04);border:1px solid var(--borde);
  border-radius:11px;padding:.65rem .9rem;cursor:pointer}
.fila.activa{border-color:var(--cyan)}
.fila .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.fila .e{font-size:.8rem;color:var(--tenue);white-space:nowrap}
.acciones{display:flex;gap:.7rem;margin-top:1rem;flex-wrap:wrap}
.acciones .btn-pri{flex:1}
/* resultado */
#resultado{display:none;flex-direction:column;gap:.9rem}
.res-cab{display:flex;align-items:center;justify-content:space-between;gap:.6rem;flex-wrap:wrap}
.tabs{display:flex;gap:.3rem;background:rgba(255,255,255,.05);padding:.25rem;border-radius:10px}
.tab{padding:.4rem .9rem;border-radius:8px;cursor:pointer;font-size:.85rem;color:var(--tenue);border:none;background:none;font-family:inherit}
.tab.on{background:rgba(255,255,255,.12);color:var(--texto)}
#nombreNota{flex:1;min-width:200px}
textarea{width:100%;min-height:460px;background:var(--code);border:1px solid var(--borde);border-radius:12px;padding:1rem;
  color:#d1d9e6;font:400 .86rem/1.6 'JetBrains Mono',monospace;resize:vertical}
textarea:focus{outline:none;border-color:rgba(0,210,255,.4)}
#vista{display:none;background:#fff;color:#1f2937;border-radius:12px;padding:1.5rem 2rem;max-height:620px;overflow:auto;line-height:1.65}
#vista h1,#vista h2,#vista h3,#vista h4{margin:1.1em 0 .5em;line-height:1.3}
#vista p,#vista ul,#vista ol,#vista table,#vista blockquote{margin:.6em 0}
#vista ul,#vista ol{padding-left:1.6em}
#vista table{border-collapse:collapse;width:100%;font-size:.9rem}
#vista th,#vista td{border:1px solid #d1d5db;padding:.4em .6em;text-align:left}
#vista th{background:#f3f4f6}
#vista blockquote{border-left:4px solid #f59e0b;padding:.4em 1em;background:#fffbeb}
#vista .img-ph{display:inline-block;background:#eef2ff;color:#4338ca;padding:.3em .7em;border-radius:6px;font-size:.85rem}
.avisos{font-size:.88rem;color:var(--warn)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(120px);background:#1a2233;color:#fff;
  padding:.75rem 1.3rem;border-radius:10px;border:1px solid rgba(255,255,255,.15);box-shadow:0 10px 25px rgba(0,0,0,.5);
  font-size:.9rem;transition:transform .3s cubic-bezier(.175,.885,.32,1.275);z-index:100;max-width:90vw}
.toast.show{transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div class="contenedor">
  <header>
    <div>
      <h1>PDF → Obsidian</h1>
      <div class="sub">Convierte PDFs a Markdown con títulos, listas, tablas e imágenes, y guárdalos en tu bóveda.</div>
    </div>
    <button class="btn btn-sec" id="btnConfig">⚙️ Bóveda de Obsidian</button>
  </header>

  <section class="card" id="panelConfig">
    <div class="grid">
      <label style="grid-column:1/-1">Carpeta de tu bóveda de Obsidian (ruta completa)
        <input type="text" id="cfgBoveda" placeholder="C:\Users\PC\Documents\MiBoveda">
      </label>
      <label>Carpeta para las notas (dentro de la bóveda)
        <input type="text" id="cfgSub" placeholder="PDFs importados">
      </label>
      <label>Carpeta para las imágenes (dentro de la anterior)
        <input type="text" id="cfgAdj" placeholder="adjuntos">
      </label>
      <label class="check"><input type="checkbox" id="cfgFront"> Añadir propiedades (título, origen, fecha, etiqueta #pdf)</label>
      <label class="check"><input type="checkbox" id="cfgImg"> Extraer imágenes del PDF</label>
      <label>Leer texto dentro de imágenes (OCR)
        <select id="cfgOcr">
          <option value="auto">Automático (Windows → Tesseract)</option>
          <option value="windows">OCR de Windows</option>
          <option value="tesseract">Tesseract</option>
          <option value="llm">IA con visión (API compatible con OpenAI, de pago)</option>
          <option value="no">Desactivado</option>
        </select>
      </label>
      <label>Idioma del OCR
        <input type="text" id="cfgIdioma" placeholder="es">
      </label>
      <label class="llm">Modelo de IA
        <input type="text" id="cfgModelo" placeholder="gpt-4o-mini">
      </label>
      <label class="llm">Clave de API
        <input type="text" id="cfgClave" placeholder="sk-...">
      </label>
      <label class="llm" style="grid-column:1/-1">URL de la API (vacío = OpenAI)
        <input type="text" id="cfgUrl" placeholder="https://api.openai.com/v1">
      </label>
    </div>
    <div class="acciones">
      <button class="btn btn-pri" id="btnGuardarCfg">Guardar configuración</button>
    </div>
    <div class="estado" id="estadoCfg"></div>
  </section>

  <section class="card">
    <div class="drop" id="drop">
      <div class="drop-icono">📄</div>
      <div class="drop-t">Arrastra aquí uno o varios PDF</div>
      <div class="drop-s">o haz clic para elegirlos · también acepta DOCX, PPTX, XLSX, HTML, TXT</div>
      <input type="file" id="archivo" multiple accept=".pdf,.docx,.pptx,.xlsx,.xls,.html,.htm,.txt,.csv,.epub">
    </div>
    <div id="lista"></div>
    <div class="acciones">
      <button class="btn btn-pri" id="btnConvertir" disabled>Convertir</button>
      <button class="btn btn-sec" id="btnGuardarTodo" disabled>💾 Guardar todo en Obsidian</button>
    </div>
  </section>

  <section class="card" id="resultado">
    <div class="res-cab">
      <input type="text" id="nombreNota" title="Nombre de la nota en Obsidian">
      <div class="tabs">
        <button class="tab on" data-v="md">Markdown</button>
        <button class="tab" data-v="vista">Vista previa</button>
      </div>
    </div>
    <div class="avisos" id="avisos"></div>
    <textarea id="editor" spellcheck="false"></textarea>
    <div id="vista"></div>
    <div class="acciones">
      <button class="btn btn-pri" id="btnGuardar">💾 Guardar en Obsidian</button>
      <a class="btn btn-sec" id="btnAbrir" style="display:none">🟣 Abrir en Obsidian</a>
      <button class="btn btn-sec" id="btnCopiar">📋 Copiar</button>
      <button class="btn btn-sec" id="btnDescargar">⬇️ Descargar .md</button>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let archivos = [];   // {file, estado, datos, guardado}
let actual = -1;

function toast(m, ms=2800){ const t=$('toast'); t.textContent=m; t.classList.add('show'); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove('show'),ms); }

// ---------- configuración
async function cargarConfig(){
  const c = await (await fetch('/api/config')).json();
  $('cfgBoveda').value=c.boveda||''; $('cfgSub').value=c.subcarpeta||''; $('cfgAdj').value=c.adjuntos||'';
  $('cfgFront').checked=!!c.frontmatter; $('cfgImg').checked=!!c.imagenes;
  $('cfgOcr').value=c.ocr||'auto'; $('cfgIdioma').value=c.ocr_idioma||'es';
  $('cfgModelo').value=c.llm_modelo||''; $('cfgClave').value=c.llm_api_key||''; $('cfgUrl').value=c.llm_url||'';
  modoLlm();
  pintarEstadoCfg(c);
  if(!c.boveda_valida) $('panelConfig').classList.add('abierto');
}
function pintarEstadoCfg(c){
  const e=$('estadoCfg');
  if(!c.boveda){ e.className='estado warn'; e.textContent='Indica la carpeta de tu bóveda para poder guardar directamente en Obsidian.'; }
  else if(!c.boveda_valida){ e.className='estado err'; e.textContent='No encuentro esa carpeta.'; }
  else if(!c.es_boveda_obsidian){ e.className='estado warn'; e.textContent='La carpeta existe, pero no tiene .obsidian: ¿seguro que es la raíz de la bóveda?'; }
  else { e.className='estado ok'; e.textContent='✓ Bóveda lista. Las notas irán a: '+c.boveda+'\\'+(c.subcarpeta||''); }
  const o=document.createElement('div'); o.className=c.ocr_activo?'ok':(c.ocr==='no'?'':'warn');
  o.textContent='OCR: '+(c.ocr_estado||''); e.appendChild(o);
}
function modoLlm(){ $('panelConfig').classList.toggle('modo-llm', $('cfgOcr').value==='llm'); }
$('cfgOcr').onchange=modoLlm;
$('btnConfig').onclick=()=>$('panelConfig').classList.toggle('abierto');
$('btnGuardarCfg').onclick=async()=>{
  const body={boveda:$('cfgBoveda').value,subcarpeta:$('cfgSub').value,adjuntos:$('cfgAdj').value||'adjuntos',
              frontmatter:$('cfgFront').checked,imagenes:$('cfgImg').checked,
              ocr:$('cfgOcr').value,ocr_idioma:$('cfgIdioma').value||'es',llm_modelo:$('cfgModelo').value,
              llm_api_key:$('cfgClave').value,llm_url:$('cfgUrl').value};
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){ $('estadoCfg').className='estado err'; $('estadoCfg').textContent=d.detail; return; }
  pintarEstadoCfg(d); toast('Configuración guardada');
};

// ---------- archivos
const drop=$('drop');
drop.onclick=()=>$('archivo').click();
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('sobre')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('sobre')}));
drop.addEventListener('drop',e=>agregar(e.dataTransfer.files));
$('archivo').onchange=e=>{agregar(e.target.files); e.target.value='';};

function agregar(lista){
  for(const f of lista) archivos.push({file:f,estado:'pendiente',datos:null,guardado:null});
  pintarLista();
}
function tam(b){ if(b<1024)return b+' B'; if(b<1048576)return (b/1024).toFixed(1)+' KB'; return (b/1048576).toFixed(1)+' MB'; }
function pintarLista(){
  const L=$('lista'); L.innerHTML='';
  archivos.forEach((a,i)=>{
    const d=document.createElement('div'); d.className='fila'+(i===actual?' activa':'');
    const icon={pendiente:'⏳',convirtiendo:'🔄',listo:'✅',error:'❌'}[a.estado];
    const extra=a.guardado?' · guardado':'';
    d.innerHTML=`<span>${icon}</span><span class="n"></span><span class="e">${tam(a.file.size)} · ${a.estado}${extra}</span>`;
    d.querySelector('.n').textContent=a.file.name;
    d.onclick=()=>{ if(a.datos) mostrar(i); };
    L.appendChild(d);
  });
  $('btnConvertir').disabled=!archivos.some(a=>a.estado==='pendiente'||a.estado==='error');
  $('btnGuardarTodo').disabled=!archivos.some(a=>a.datos&&!a.guardado);
}

$('btnConvertir').onclick=async()=>{
  $('btnConvertir').disabled=true;
  for(let i=0;i<archivos.length;i++){
    const a=archivos[i]; if(a.estado==='listo') continue;
    a.estado='convirtiendo'; pintarLista();
    const fd=new FormData(); fd.append('file',a.file);
    try{
      const r=await fetch('/api/convert',{method:'POST',body:fd});
      const d=await r.json();
      if(!r.ok) throw new Error(d.detail||'Error');
      a.datos=d; a.estado='listo';
      if(actual<0||!archivos[actual].datos) mostrar(i);
    }catch(err){ a.estado='error'; a.error=err.message; toast('❌ '+a.file.name+': '+err.message,5000); }
    pintarLista();
  }
};

// ---------- resultado
function guardarEdicion(){ if(actual>=0&&archivos[actual].datos){ archivos[actual].datos.markdown=$('editor').value; archivos[actual].datos.nombre=$('nombreNota').value; } }
function mostrar(i){
  guardarEdicion(); actual=i; const d=archivos[i].datos;
  $('resultado').style.display='flex';
  $('editor').value=d.markdown; $('nombreNota').value=d.nombre;
  const av=[...(d.avisos||[])]; if(d.imagenes&&d.imagenes.length) av.unshift(`🖼️ ${d.imagenes.length} imagen(es) extraídas; se copiarán a la carpeta de adjuntos al guardar.`);
  $('avisos').textContent=av.join('  ·  ');
  const g=archivos[i].guardado; $('btnAbrir').style.display=g?'inline-flex':'none'; if(g) $('btnAbrir').href=g.obsidian_uri;
  if(document.querySelector('.tab.on').dataset.v==='vista') renderVista();
  pintarLista();
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on')); t.classList.add('on');
  const v=t.dataset.v; $('editor').style.display=v==='md'?'block':'none'; $('vista').style.display=v==='vista'?'block':'none';
  if(v==='vista') renderVista();
});
function renderVista(){
  let md=$('editor').value.replace(/^---\n[\s\S]*?\n---\n/,'');
  md=md.replace(/!\[\[([^\]]+)\]\]/g,'<span class="img-ph">🖼️ $1</span>');
  md=md.replace(/^> \[!(\w+)\]\s*/gm,'> **$1:** ');
  $('vista').innerHTML = window.marked ? marked.parse(md) : '<pre>'+md.replace(/</g,'&lt;')+'</pre>';
}

async function guardar(i){
  const a=archivos[i]; if(!a.datos) return false;
  const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:a.datos.id,nombre:a.datos.nombre,markdown:a.datos.markdown,sobrescribir:!!a.guardado})});
  const d=await r.json();
  if(!r.ok){ toast('❌ '+d.detail,5000); if(r.status===400) $('panelConfig').classList.add('abierto'); return false; }
  a.guardado=d; return true;
}
$('btnGuardar').onclick=async()=>{
  guardarEdicion();
  if(await guardar(actual)){ const g=archivos[actual].guardado; toast('✅ Guardado en '+g.relativa); mostrar(actual); }
};
$('btnGuardarTodo').onclick=async()=>{
  guardarEdicion(); let n=0;
  for(let i=0;i<archivos.length;i++) if(archivos[i].datos&&!archivos[i].guardado&&await guardar(i)) n++;
  pintarLista(); if(actual>=0) mostrar(actual); if(n) toast(`✅ ${n} nota(s) guardadas en Obsidian`);
};
$('btnCopiar').onclick=()=>navigator.clipboard.writeText($('editor').value).then(()=>toast('Markdown copiado'));
$('btnDescargar').onclick=()=>{
  const b=new Blob([$('editor').value],{type:'text/markdown;charset=utf-8'}); const u=URL.createObjectURL(b);
  const a=document.createElement('a'); a.href=u; a.download=($('nombreNota').value||'documento')+'.md'; a.click(); URL.revokeObjectURL(u);
};
cargarConfig();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
