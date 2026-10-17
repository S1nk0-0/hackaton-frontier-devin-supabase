"""Flujo de caja y estimación de cuota. No necesita API.

Uso rápido:  python -m src.caja data/ejemplos_respuestas.csv   (debe dar 788.90)
"""
from __future__ import annotations

import csv
import sys

SEMANAS_POR_MES = 4.3
PORCENTAJE_CUOTA = 0.30


def _monto(fila: dict) -> float | None:
    valor = fila.get("monto")
    if valor is None or str(valor).strip() == "":
        return None
    try:
        return float(str(valor).replace("S/", "").replace(",", ".").strip())
    except ValueError:
        return None


def _confirmada(fila: dict) -> bool:
    return str(fila.get("requiere_confirmacion", "NO")).strip().upper() != "SI"


def flujo_caja(filas: list[dict]) -> dict:
    """Flujo = ventas + cobros_fiado − gastos. Los fiados no mueven la caja."""
    tot = {"venta": 0.0, "cobro_fiado": 0.0, "gasto": 0.0, "fiado": 0.0}
    for f in filas:
        m = _monto(f)
        tipo = str(f.get("tipo", "")).strip()
        if m is not None and tipo in tot:
            tot[tipo] += m
    flujo = tot["venta"] + tot["cobro_fiado"] - tot["gasto"]
    return {
        "ventas": round(tot["venta"], 2),
        "cobros_fiado": round(tot["cobro_fiado"], 2),
        "gastos": round(tot["gasto"], 2),
        "fiados_pendientes": round(tot["fiado"], 2),
        "flujo": round(flujo, 2),
    }


def estimar_cuota(filas: list[dict], semanas: int = 1) -> tuple[float, str]:
    """Cuota conservadora = flujo semanal × 4.3 × 0.30, sin las filas por confirmar.

    Es SOLO una estimación orientativa: no es una oferta ni una aprobación de crédito.
    """
    confirmadas = [f for f in filas if _confirmada(f)]
    sin_confirmar = len(filas) - len(confirmadas)
    c = flujo_caja(confirmadas)
    semanal = c["flujo"] / max(semanas, 1)
    aviso = (
        "Ojo: esto es solo una estimación para que te organices. "
        "No es una aprobación de crédito; eso lo ve siempre tu asesor de Mibanco."
    )
    if semanal <= 0:
        return 0.0, (
            f"Esta semana lo que salió de tu caja (S/ {c['gastos']:.2f}) fue igual o más que lo que entró "
            f"(S/ {c['ventas'] + c['cobros_fiado']:.2f}). Por ahora no te conviene asumir una cuota. {aviso}"
        )
    mensual = semanal * SEMANAS_POR_MES
    cuota = round(mensual * PORCENTAJE_CUOTA, 2)
    texto = (
        f"En una semana te quedan libres unos S/ {semanal:.2f} "
        f"(vendiste y cobraste S/ {(c['ventas'] + c['cobros_fiado']) / max(semanas, 1):.2f} "
        f"y gastaste S/ {c['gastos'] / max(semanas, 1):.2f}). "
        f"En un mes serían unos S/ {mensual:.2f}. Para que no te ajustes, "
        f"lo prudente es usar solo 3 de cada 10 soles para la cuota: más o menos S/ {cuota:.2f} al mes."
    )
    if sin_confirmar:
        texto += f" No conté {sin_confirmar} operación(es) que todavía no me confirmaste."
    return cuota, texto + " " + aviso


if __name__ == "__main__":
    ruta = sys.argv[1] if len(sys.argv) > 1 else "data/ejemplos_respuestas.csv"
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        filas = list(csv.DictReader(fh))
    print(flujo_caja(filas))
    monto, explicacion = estimar_cuota(filas)
    print(f"Cuota estimada: S/ {monto:.2f}\n{explicacion}")
