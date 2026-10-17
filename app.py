"""App para Doña Rosa: de la libreta a la caja.

    python app.py   ->  http://127.0.0.1:7860
"""
from __future__ import annotations

import copy
import csv

import gradio as gr
import pandas as pd

from src.caja import estimar_cuota, flujo_caja
from src.motor import COLUMNAS, a_fila_csv, completar_pendiente, nuevo_estado, procesar, procesar_lote
from src.voz import transcribir

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

def responder(texto: str, historial: list, estado: dict):
    texto = (texto or "").strip()
    if not texto:
        return historial, estado, tabla(estado), resumen(estado)
    historial = historial + [{"role": "user", "content": texto}]

    # 1) ¿Estaba esperando un monto? La respuesta completa esa fila.
    fila = completar_pendiente(estado, texto)
    if fila:
        respuesta = f"¡Gracias! Lo anoté. {describir(fila)}"
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
    historial = historial + [{"role": "assistant", "content": respuesta}]
    return historial, estado, tabla(estado), resumen(estado)


def responder_audio(audio: str | None, historial: list, estado: dict):
    if not audio:
        return historial, estado, tabla(estado), resumen(estado), None
    try:
        texto = transcribir(audio)
    except Exception as e:  # noqa: BLE001
        historial = historial + [{"role": "assistant", "content": f"No pude escuchar bien el audio 😕 ({e})"}]
        return historial, estado, tabla(estado), resumen(estado), None
    return (*responder(f"🎤 {texto}", historial, estado), None)


def cuota(historial: list, estado: dict):
    filas = estado["semana_previa"] + [a_fila_csv(f) for f in estado["transacciones"]]
    semanas = (1 if estado["semana_previa"] else 0) + (1 if estado["transacciones"] else 0)
    if not filas:
        msg = "Todavía no tengo movimientos tuyos. Cuéntame qué vendiste y gastaste y te ayudo a calcular. 🙂"
    else:
        monto, explicacion = estimar_cuota(filas, semanas=max(semanas, 1))
        msg = f"### 🧮 Podrías pagar unos S/ {monto:.2f} al mes\n{explicacion}"
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
