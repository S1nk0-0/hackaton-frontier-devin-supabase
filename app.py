"""App para Doña Rosa: de la libreta a la caja.

    python app.py   ->  http://127.0.0.1:7860
"""
from __future__ import annotations

import copy
import csv
import os
import re

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
    "¡Hola, Doña Rosa! 👋 Cuéntame lo que vendiste, compraste o fiaste, como lo anotarías en tu libreta. "
    "Puedes escribir o mandarme una nota de voz. Por ejemplo: *vendí 3 gaseosas a 2.50*."
)


def estado_inicial() -> dict:
    e = nuevo_estado()
    e.update({"contador": 0, "semana_previa": []})
    return e


# --------------------------------------------------------------------------- vistas

def tabla(estado: dict) -> pd.DataFrame:
    filas = [{"semana": "anterior", **f} for f in estado["semana_previa"]]
    filas += [{"semana": "esta", **a_fila_csv(f)} for f in estado["transacciones"]]
    return pd.DataFrame(filas, columns=["semana", *COLUMNAS])


def resumen(estado: dict) -> str:
    c = flujo_caja([a_fila_csv(f) for f in estado["transacciones"]])
    txt = (
        f"### 💰 Tu caja de esta semana\n"
        f"- Vendiste: **S/ {c['ventas']:.2f}**\n"
        f"- Te pagaron fiados: **S/ {c['cobros_fiado']:.2f}**\n"
        f"- Gastaste: **S/ {c['gastos']:.2f}**\n"
        f"- Te quedan libres: **S/ {c['flujo']:.2f}**\n"
    )
    if estado["fiados"]:
        deudas = ", ".join(f"{k.title()}: S/ {v:.2f}" for k, v in estado["fiados"].items())
        txt += f"\n📒 Te deben: {deudas}"
    if estado["semana_previa"]:
        p = flujo_caja(estado["semana_previa"])
        txt += f"\n\n_Semana anterior cargada: te quedaron S/ {p['flujo']:.2f}_"
    return txt


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
            respuesta = f"Uy, Doña Rosa, no pude conectarme ahorita 😕. Inténtalo otra vez en un ratito. ({e})"
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
            msg = f"🎤 Uy, no pude escuchar el audio ahorita 😕. ¿Me lo mandas otra vez o me lo escribes? ({e})"
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

with gr.Blocks(title="De la libreta a la caja") as demo:
    estado = gr.State(copy.deepcopy(estado_inicial()))
    gr.Markdown("# 📒 De la libreta a la caja\nTu cuaderno de ventas, pero en el celular.")
    if MODO_PRUEBA:
        gr.Markdown("⚠️ _Modo prueba: sin GROQ_API_KEY se usa un motor simple sin IA y la voz no funciona._")
    with gr.Row():
        with gr.Column(scale=3):
            chat = gr.Chatbot(value=[{"role": "assistant", "content": BIENVENIDA}], height=450, label="Conversación")
            with gr.Row():
                caja_texto = gr.Textbox(placeholder="Escribe aquí… ej: vendí 2 kilos de arroz a 4 soles", show_label=False, scale=4)
                enviar = gr.Button("Enviar", variant="primary", scale=1)
            audio = gr.Audio(sources=["microphone"], type="filepath", label="🎤 O mándame una nota de voz")
            boton_cuota = gr.Button("¿Cuánto puedo pagar de cuota?", variant="secondary", size="lg")
        with gr.Column(scale=2):
            panel = gr.Markdown(resumen(estado_inicial()))
            with gr.Accordion("📂 Cargar semana anterior (CSV)", open=False):
                archivo = gr.File(file_types=[".csv"], label="CSV")
                estado_carga = gr.Markdown()
    tabla_tx = gr.Dataframe(value=tabla(estado_inicial()), label="Mis movimientos", interactive=False)

    salidas = [chat, estado, tabla_tx, panel]
    enviar.click(responder, [caja_texto, chat, estado], salidas).then(lambda: "", None, caja_texto)
    caja_texto.submit(responder, [caja_texto, chat, estado], salidas).then(lambda: "", None, caja_texto)
    audio.stop_recording(responder_audio, [audio, chat, estado], [*salidas, audio])
    boton_cuota.click(cuota, [chat, estado], chat)
    archivo.upload(cargar_semana, [archivo, estado], [estado, tabla_tx, panel, estado_carga])


if __name__ == "__main__":
    demo.launch()
