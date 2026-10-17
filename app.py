"""App para Doña Rosa: de la libreta a la caja.

    python app.py   ->  http://127.0.0.1:7860
"""
from __future__ import annotations

import copy
import csv
import os
import re
from html import escape

import gradio as gr
import pandas as pd

from src.caja import estimar_cuota, flujo_caja
from src.motor import COLUMNAS, a_fila_csv, completar_pendiente, nuevo_estado
from src.numeros import CENTENAS, DECENAS, RELLENO, UNIDADES, _norm, texto_a_monto
from src.voz import transcribir

# Sin key (o con MOCK_MOTOR=1) la app usa un motor de prueba sin LLM, para avanzar la interfaz.
MODO_PRUEBA = os.getenv("MOCK_MOTOR") == "1" or not os.getenv("GROQ_API_KEY")
if MODO_PRUEBA:
    from src.motor_mock import procesar, procesar_lote
else:
    from src.motor import procesar, procesar_lote

NO_SE = re.compile(r"\b(no se|no me acuerdo|no recuerdo|despues|luego|ni idea)\b")
PORCENTAJE_MINIMO = 0.25  # la cuota se muestra como rango 25 %–30 % del flujo mensual
VERBO_OPERACION = re.compile(r"^(vend|bend|pagu|pago|compr|cobr|devolv|gast|sali|fie|fio|fia)")
PALABRAS_RESPUESTA = RELLENO | set(UNIDADES) | set(DECENAS) | set(CENTENAS) | {
    "mil", "me", "le", "lo", "la", "el", "los", "las", "solo", "nada", "mas", "menos", "todo"}

BIENVENIDA = (
    "¡Hola, Doña Rosa! 👋 Yo soy tu libreta.\n\n"
    "Cuéntame lo que **vendiste**, **compraste** o **fiaste**, como lo apuntarías en tu cuaderno. "
    "Puedes **escribirme** o **mandarme un audio** 🎤.\n\n"
    "Por ejemplo: *vendí 3 gaseosas a 2.50*"
)
TIPOS_BONITOS = {"venta": "🛒 Venta", "gasto": "🧾 Gasto", "fiado": "📒 Fiado", "cobro_fiado": "🤝 Me pagaron"}


def estado_inicial() -> dict:
    e = nuevo_estado()
    e.update({"contador": 0, "semana_previa": []})
    return e


# --------------------------------------------------------------------------- vistas

COLUMNAS_TABLA = ["Semana", "Qué fue", "Producto", "Cantidad", "Monto", "¿Listo?"]


def tabla(estado: dict) -> pd.DataFrame:
    """Tabla en palabras sencillas (el CSV de entrega con el formato exacto lo genera batch.py)."""
    filas = [("Pasada", f) for f in estado["semana_previa"]]
    filas += [("Esta", a_fila_csv(f)) for f in estado["transacciones"]]
    return pd.DataFrame(
        [
            {
                "Semana": semana,
                "Qué fue": TIPOS_BONITOS.get(f["tipo"], f["tipo"]),
                "Producto": f["producto"],
                "Cantidad": f["cantidad"],
                "Monto": f"S/ {f['monto']}" if f["monto"] else "—",
                "¿Listo?": "✅ Sí" if f["requiere_confirmacion"] != "SI" else "❓ Falta el monto",
            }
            for semana, f in filas
        ],
        columns=COLUMNAS_TABLA,
    )


def resumen(estado: dict) -> str:
    c = flujo_caja([a_fila_csv(f) for f in estado["transacciones"]])
    tarjetas = [
        ("🛒", "Vendiste", c["ventas"], ""),
        ("🤝", "Te pagaron lo fiado", c["cobros_fiado"], ""),
        ("🧾", "Gastaste", c["gastos"], "negativo"),
    ]
    html = '<div class="caja"><h2>💰 Tu caja de esta semana</h2><div class="tarjetas">'
    for icono, nombre, monto, clase in tarjetas:
        html += f'<div class="tarjeta {clase}"><span>{icono} {nombre}</span><strong>S/ {monto:.2f}</strong></div>'
    html += "</div>"
    clase = "queda rojo" if c["flujo"] < 0 else "queda"
    html += f'<div class="{clase}"><span>Te queda libre</span><strong>S/ {c["flujo"]:.2f}</strong></div>'
    if estado["fiados"]:
        html += '<div class="deben"><h3>📒 Te deben</h3><ul>'
        for k, v in estado["fiados"].items():
            html += f"<li><span>{escape(k.title())}</span><strong>S/ {v:.2f}</strong></li>"
        html += "</ul></div>"
    if estado["semana_previa"]:
        p = flujo_caja(estado["semana_previa"])
        html += f'<p class="nota">La semana pasada te quedaron S/ {p["flujo"]:.2f}</p>'
    return html + "</div>"


def describir(fila: dict) -> str:
    nombres = {"venta": "Venta", "gasto": "Gasto", "fiado": "Fiado", "cobro_fiado": "Cobro de fiado"}
    monto = "monto por confirmar" if fila["monto"] is None else f"S/ {fila['monto']:.2f}"
    cant = f"{a_fila_csv(fila)['cantidad']} " if fila.get("cantidad") else ""
    return f"✅ {nombres[fila['tipo']]}: {cant}{fila['producto']} — {monto}"


# --------------------------------------------------------------------------- acciones

def es_operacion_nueva(texto: str) -> bool:
    """Con una pregunta pendiente, distingue "fueron 12" / "pagué 5 soles" (respuesta)
    de "vendí 4 gaseosas a 2.50" / "don Pepe me pagó 10" (mensaje nuevo)."""
    palabras = re.findall(r"[a-zñ]+", _norm(texto))
    if not any(VERBO_OPERACION.match(p) for p in palabras):
        return False
    return any(not VERBO_OPERACION.match(p) and p not in PALABRAS_RESPUESTA for p in palabras)


def responder(texto: str, historial: list, estado: dict, mostrar: str | None = None):
    """`mostrar` es lo que se ve en el chat (p. ej. la nota de voz); `texto` es lo que se procesa."""
    texto = (texto or "").strip()
    if not texto:
        return historial, estado, tabla(estado), resumen(estado)
    historial = historial + [{"role": "user", "content": mostrar or texto}]

    # 1) ¿Estaba esperando un monto? La respuesta completa esa fila.
    #    Si en vez de responder cuenta otra operación, se procesa como mensaje nuevo.
    fila = None
    pendiente_previo = estado["pendiente"]
    if pendiente_previo and not es_operacion_nueva(texto):
        if NO_SE.search(_norm(texto)):
            estado["pendiente"]["fila"]["_omitida"] = True  # sigue con SI, pero no se vuelve a preguntar
            estado["pendiente"] = None
            respuesta = "No te preocupes 🙂. Lo dejo marcado para confirmarlo después y no lo cuento en tu caja."
            respuesta += siguiente_pregunta(estado)
            historial = historial + [{"role": "assistant", "content": respuesta}]
            return historial, estado, tabla(estado), resumen(estado)
        monto = texto_a_monto(texto)  # entiende "12.50" y también "seis cincuenta"
        if monto is not None:
            fila = completar_pendiente(estado, f"{monto}")
    if fila:
        respuesta = f"¡Gracias! Lo anoté. {describir(fila)}" + siguiente_pregunta(estado)
    else:
        # 2) Mensaje nuevo
        estado["contador"] += 1
        try:
            r = procesar(f"app-{estado['contador']}", texto, estado)
        except Exception as e:  # noqa: BLE001
            r = None
            print(f"[app] error del motor: {e}")  # el detalle técnico va a la consola, no a Doña Rosa
            respuesta = "Uy, Doña Rosa, no pude conectarme ahorita 😕. Inténtalo otra vez en un ratito, por favor."
        if r is not None:
            if r["filas"]:
                respuesta = "\n".join(describir(f) for f in r["filas"])
            else:
                respuesta = "Esto parece un gasto de la casa, no del negocio, así que no lo anoté en la caja. 🙂"
            if r["pregunta"]:
                respuesta += f"\n\n❓ {r['pregunta']}"
            elif pendiente_previo and estado["pendiente"] is pendiente_previo:
                respuesta += f"\n\n❓ Y lo de antes: {pendiente_previo['pregunta']}"
    historial = historial + [{"role": "assistant", "content": respuesta}]
    return historial, estado, tabla(estado), resumen(estado)


def siguiente_pregunta(estado: dict) -> str:
    """Si quedan otras filas sin monto, pregunta por la siguiente (de una en una)."""
    for f in estado["transacciones"]:
        if f["monto"] is None and f["requiere_confirmacion"] == "SI" and not f.get("_omitida"):
            pregunta = f"¿Y cuánto fue lo de {f['producto'] or 'la otra operación'}?"
            estado["pendiente"] = {"fila": f, "pregunta": pregunta}
            return f"\n\n❓ {pregunta}"
    return ""


def responder_audio(audio: str | None, historial: list, estado: dict):
    if not audio:
        return historial, estado, tabla(estado), resumen(estado), None
    try:
        texto = transcribir(audio)
    except Exception as e:  # noqa: BLE001
        if "GROQ_API_KEY" in str(e):
            msg = "🎤 En modo prueba no puedo escuchar audios. Escríbeme tu mensaje, por favor. 🙂"
        else:
            print(f"[app] error de voz: {e}")
            msg = "🎤 Uy, no pude escuchar el audio ahorita 😕. ¿Me lo mandas otra vez o me lo escribes?"
        historial = historial + [{"role": "assistant", "content": msg}]
        return historial, estado, tabla(estado), resumen(estado), None
    if not texto:  # silencio o solo ruido
        historial = historial + [{"role": "assistant", "content": "🎤 No te escuché bien, Doña Rosa. ¿Me lo repites un poquito más cerca del celular?"}]
        return historial, estado, tabla(estado), resumen(estado), None
    return (*responder(texto, historial, estado, mostrar=f"🎤 *Te entendí:* {texto}"), None)


def cuota(historial: list, estado: dict):
    filas = estado["semana_previa"] + [a_fila_csv(f) for f in estado["transacciones"]]
    semanas = (1 if estado["semana_previa"] else 0) + (1 if estado["transacciones"] else 0)
    if not filas:
        msg = "Todavía no tengo movimientos tuyos. Cuéntame qué vendiste y gastaste y te ayudo a calcular. 🙂"
    else:
        monto, explicacion = estimar_cuota(filas, semanas=max(semanas, 1))
        if monto > 0:
            minimo = monto * PORCENTAJE_MINIMO / 0.30
            msg = f"### 🧮 Una cuota cómoda para ti estaría entre S/ {minimo:.2f} y S/ {monto:.2f} al mes\n{explicacion}"
        else:
            msg = f"### 🧮 Por ahora mejor no asumir una cuota\n{explicacion}"
    historial = historial + [
        {"role": "user", "content": "¿Cuánto puedo pagar de cuota?"},
        {"role": "assistant", "content": msg},
    ]
    return historial


def cargar_semana(archivo, estado: dict):
    """Acepta el set oculto (id_mensaje,dia,mensaje) o un CSV ya procesado (con columna tipo)."""
    if archivo is None:
        return estado, tabla(estado), resumen(estado), "No se cargó ningún archivo."
    ruta = archivo if isinstance(archivo, str) else archivo.name
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    if filas and "tipo" in filas[0]:
        estado["semana_previa"] = [{c: r.get(c, "") for c in COLUMNAS} for r in filas]
    else:
        temporal = nuevo_estado()
        res = procesar_lote([(r["id_mensaje"].strip(), r["mensaje"]) for r in filas], temporal)
        estado["semana_previa"] = [a_fila_csv(f) for r in filas for f in res[r["id_mensaje"].strip()]["filas"]]
        # los fiados de la semana pasada siguen pendientes
        for k, v in temporal["fiados"].items():
            estado["fiados"][k] = round(estado["fiados"].get(k, 0) + v, 2)
    return estado, tabla(estado), resumen(estado), f"Listo: cargué {len(estado['semana_previa'])} movimientos."


# --------------------------------------------------------------------------- interfaz
# Pensada para una señora mayor y poco digital: letra grande y muy legible, botones grandes,
# alto contraste, colores que dicen "entra" (verde) y "sale" (rojo), y poco texto técnico.

TEMA = gr.themes.Soft(
    primary_hue="green",
    secondary_hue="amber",
    neutral_hue="stone",
    text_size=gr.themes.sizes.text_lg,
    radius_size=gr.themes.sizes.radius_lg,
    spacing_size=gr.themes.sizes.spacing_lg,
    font=[gr.themes.GoogleFont("Lexend"), "system-ui", "sans-serif"],
).set(
    body_text_size="20px",
    button_large_text_size="22px",
    button_large_padding="18px 24px",
    input_text_size="21px",
    block_label_text_size="18px",
)

CSS = """
:root {
  --rosa-verde: #1d6b45; --rosa-verde-suave: #e6f4ec;
  --rosa-rojo: #a8361a; --rosa-rojo-suave: #fcebe5;
  --rosa-ambar: #f5b800; --rosa-texto: #1c2a22; --rosa-borde: #cfe0d6;
}
.dark {
  --rosa-verde-suave: #16301f; --rosa-rojo-suave: #3a1d14;
  --rosa-texto: #f0f6f2; --rosa-borde: #2d4a39;
}
.gradio-container { max-width: 1100px !important; margin: 0 auto !important; }
@media (max-width: 640px) {
  .gradio-container { padding: 0 8px !important; }
  #cabecera { padding: 14px; } #cabecera .icono { font-size: 40px; } #cabecera h1 { font-size: 26px; }
  .queda strong { font-size: 36px; }
}
* { box-sizing: border-box; }
.boton-grande { background: var(--rosa-verde) !important; color: #fff !important; border: none !important; }
footer { display: none !important; }

#cabecera { display: flex; align-items: center; gap: 16px; padding: 18px 20px; border-radius: 20px;
  background: var(--rosa-verde); color: #fff; }
#cabecera .icono { font-size: 52px; line-height: 1; }
#cabecera h1 { margin: 0; font-size: 32px; color: #fff; }
#cabecera p { margin: 4px 0 0; font-size: 20px; color: #fff; opacity: .95; }
#pasos { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
#pasos span { padding: 8px 14px; border-radius: 999px; font-size: 18px;
  background: var(--rosa-verde-suave); color: var(--rosa-texto); border: 1px solid var(--rosa-borde); }
#aviso-prueba { padding: 10px 14px; border-radius: 12px; font-size: 17px;
  background: var(--rosa-rojo-suave); color: var(--rosa-texto); }

#chat .prose, #chat .prose * { font-size: 21px !important; line-height: 1.5 !important; }
#entrada textarea { font-size: 22px !important; min-height: 64px !important; }
.boton-grande { min-height: 64px !important; font-size: 22px !important; font-weight: 700 !important; }
#boton-cuota { min-height: 80px !important; font-size: 24px !important; font-weight: 800 !important;
  background: var(--rosa-ambar) !important; color: #1c2a22 !important; border: none !important; }
#audio label, #audio span { font-size: 19px !important; }

.caja h2 { margin: 0 0 12px; font-size: 26px; color: var(--rosa-texto); }
.tarjetas { display: grid; gap: 10px; }
.tarjeta { display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 14px 18px; border-radius: 16px; font-size: 20px;
  background: var(--rosa-verde-suave); color: var(--rosa-texto); border: 1px solid var(--rosa-borde); }
.tarjeta strong { font-size: 24px; white-space: nowrap; }
.tarjeta.negativo { background: var(--rosa-rojo-suave); }
.queda { margin-top: 12px; padding: 18px; border-radius: 18px; text-align: center;
  background: var(--rosa-verde); color: #fff; }
.queda span, .queda strong { display: block; color: #fff !important; }
.queda span { font-size: 20px; }
.queda strong { font-size: 42px; line-height: 1.2; }
.queda.rojo { background: var(--rosa-rojo); }
.deben { margin-top: 14px; padding: 14px 18px; border-radius: 16px;
  border: 2px dashed var(--rosa-borde); color: var(--rosa-texto); }
.deben h3 { margin: 0 0 8px; font-size: 22px; }
.deben ul { list-style: none; margin: 0; padding: 0; }
.deben li { display: flex; justify-content: space-between; font-size: 20px; padding: 4px 0; }
.nota { margin-top: 10px; font-size: 18px; color: var(--rosa-texto); opacity: .8; }
#tabla table { font-size: 19px !important; }
"""

with gr.Blocks(title="Mi libreta de caja") as demo:
    estado = gr.State(copy.deepcopy(estado_inicial()))
    gr.HTML(
        '<div id="cabecera"><div class="icono">📒</div><div>'
        "<h1>Mi libreta de caja</h1>"
        "<p>Cuéntame tus ventas y gastos, yo los apunto por ti.</p></div></div>"
        '<div id="pasos"><span>1️⃣ Me cuentas</span><span>2️⃣ Yo lo apunto</span>'
        "<span>3️⃣ Ves cuánto te queda</span></div>"
    )
    if MODO_PRUEBA:
        gr.HTML('<div id="aviso-prueba">⚠️ Modo prueba: no hay GROQ_API_KEY, uso un motor simple sin IA y sin voz.</div>')
    with gr.Row(equal_height=False):
        with gr.Column(scale=3, min_width=280):
            chat = gr.Chatbot(
                value=[{"role": "assistant", "content": BIENVENIDA}],
                height=460, label="Conversación", show_label=False, elem_id="chat", layout="bubble",
            )
            caja_texto = gr.Textbox(
                placeholder="Escribe aquí… por ejemplo: vendí 2 kilos de arroz a 4 soles",
                show_label=False, lines=1, max_lines=3, elem_id="entrada",
            )
            enviar = gr.Button("✉️  Enviar", variant="primary", size="lg", elem_classes="boton-grande")
            audio = gr.Audio(
                sources=["microphone"], type="filepath", elem_id="audio",
                label="🎤 ¿Prefieres hablar? Aprieta «Grabar», cuéntame y vuelve a apretar para terminar",
            )
        with gr.Column(scale=2, min_width=280):
            panel = gr.HTML(resumen(estado_inicial()))
            boton_cuota = gr.Button("🧮  ¿Cuánto puedo pagar de cuota?", size="lg", elem_id="boton-cuota")
    with gr.Accordion("📋 Ver todo lo que apunté", open=False):
        tabla_tx = gr.Dataframe(value=tabla(estado_inicial()), show_label=False, interactive=False, elem_id="tabla")
    with gr.Accordion("📂 Cargar la semana pasada (archivo CSV)", open=False):
        archivo = gr.File(file_types=[".csv"], label="Archivo CSV")
        estado_carga = gr.Markdown()

    salidas = [chat, estado, tabla_tx, panel]
    enviar.click(responder, [caja_texto, chat, estado], salidas).then(lambda: "", None, caja_texto)
    caja_texto.submit(responder, [caja_texto, chat, estado], salidas).then(lambda: "", None, caja_texto)
    audio.stop_recording(responder_audio, [audio, chat, estado], [*salidas, audio])
    boton_cuota.click(cuota, [chat, estado], chat)
    archivo.upload(cargar_semana, [archivo, estado], [estado, tabla_tx, panel, estado_carga])


if __name__ == "__main__":
    demo.launch(theme=TEMA, css=CSS)
