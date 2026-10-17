"""Motor de prueba (sin LLM) para desarrollar la interfaz sin API key.

Mismo contrato que src/motor.py. Entiende frases simples tipo "vendí 3 gaseosas a 2.50",
"pagué el agua 45", "le fié 2 cervezas a don Pepe", "don Pepe me pagó". Se activa en app.py
cuando no hay GROQ_API_KEY o con MOCK_MOTOR=1.
"""
from __future__ import annotations

import re

from src.motor import _aplicar
from src.numeros import _norm, texto_a_monto

_PRECIO = re.compile(r"\ba\s+(\d+(?:[.,]\d+)?)")
_CANT_PROD = re.compile(r"(\d+)\s+(?:kilos?\s+de\s+|paquetes?\s+de\s+)?([a-zñ]+)")
_VERBOS = {"pague", "compre", "vendi", "gaste", "devolvi", "salio", "salieron", "hoy", "total"}
_CLIENTE = re.compile(r"\b(?:senora|dona|don)\s+([a-z]+(?:\s+maria)?)")


def _extraer(texto: str) -> dict:
    t = _norm(texto)
    if re.search(r"para la casa|no es del negocio|personal", t):
        return {"filas": []}
    if re.search(r"me pago|me pagaron|me cancelo", t):
        tipo = "cobro_fiado"
    elif re.search(r"\bfi[eo]\b|fiado", t):
        tipo = "fiado"
    elif re.search(r"pague|compre|devolvi|gaste", t):
        tipo = "gasto"
    else:
        tipo = "venta"

    cp = _CANT_PROD.search(t)
    cantidad = float(cp.group(1)) if cp else None
    producto = cp.group(2) if cp else ""
    precio = _PRECIO.search(t)
    fila = {"tipo": tipo, "producto": producto, "cantidad": cantidad}
    if precio:
        fila["precio_unitario"] = float(precio.group(1).replace(",", "."))
    else:
        resto = t.replace(cp.group(0), "") if cp else t
        fila["monto_total"] = texto_a_monto(resto)
        if not producto:
            fila["producto"] = next((w for w in resto.split() if len(w) > 3 and w.isalpha() and w not in _VERBOS), "")
    cliente = _CLIENTE.search(t)
    if tipo in {"fiado", "cobro_fiado"} and cliente:
        fila["cliente"] = cliente.group(1)
    return {"filas": [fila]}


def procesar(id_mensaje: str, texto: str, estado: dict) -> dict:
    return _aplicar(str(id_mensaje), _extraer(texto), estado)


def procesar_lote(mensajes: list[tuple[str, str]], estado: dict, tam: int = 10) -> dict[str, dict]:
    return {mid: procesar(mid, texto, estado) for mid, texto in mensajes}
