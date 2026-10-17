"""Genera la entrega a partir del set oculto.

    python scripts/batch.py set_oculto.csv equipo      -> resultados_equipo.csv
    python scripts/batch.py set_oculto.csv equipo --por-mensaje   (más lento, 1 llamada por mensaje)

Entrada: id_mensaje,dia,mensaje. Salida: id_mensaje,tipo,producto,cantidad,monto,requiere_confirmacion
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.caja import flujo_caja  # noqa: E402
from src.motor import COLUMNAS, DATA, a_fila_csv, nuevo_estado, procesar, procesar_lote  # noqa: E402


def columnas_plantilla() -> list[str]:
    ruta = DATA / "plantilla_entrega.csv"
    if ruta.exists():
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            cab = next(csv.reader(f), None)
        if cab:
            return [c.strip() for c in cab]
    return COLUMNAS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("equipo")
    ap.add_argument("--por-mensaje", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    with open(args.entrada, encoding="utf-8-sig", newline="") as f:
        mensajes = [(r["id_mensaje"].strip(), r["mensaje"]) for r in csv.DictReader(f)]

    estado = nuevo_estado()
    if args.por_mensaje:
        res = {mid: procesar(mid, texto, estado) for mid, texto in mensajes}
    else:
        res = procesar_lote(mensajes, estado)

    filas = [a_fila_csv(f) for mid, _ in mensajes for f in res[mid]["filas"]]
    salida = Path(f"resultados_{args.equipo}.csv")
    cols = columnas_plantilla()
    with open(salida, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)

    print(f"{len(mensajes)} mensajes -> {len(filas)} filas en {salida} ({time.time() - t0:.1f}s)")
    print("Flujo:", flujo_caja(filas))


if __name__ == "__main__":
    main()
