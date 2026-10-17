"""Evalúa el motor contra data/ejemplos_respuestas.csv.

    python scripts/evaluar.py            # mensaje por mensaje (como la app)
    python scripts/evaluar.py --lote     # en bloques (como batch.py, más rápido)

Nota: al procesar cada mensaje se quita ESE mensaje de los few-shot, para no hacer trampa.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.caja import flujo_caja  # noqa: E402
from src.motor import DATA, _norm, a_fila_csv, nuevo_estado, procesar, procesar_lote  # noqa: E402

FLUJO_ESPERADO = 788.90


def leer(ruta: Path) -> list[dict]:
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clave(f: dict, con_producto: bool = True) -> tuple:
    monto = f.get("monto", "")
    monto = "" if str(monto).strip() == "" else round(float(monto), 2)
    cant = str(f.get("cantidad", "")).strip()
    cant = "" if cant == "" else round(float(cant), 3)
    base = (f["tipo"].strip(), cant, monto, f["requiere_confirmacion"].strip().upper())
    return base + ((_norm(f["producto"]),) if con_producto else ())


def coincidencias(esperadas: list[dict], obtenidas: list[dict], con_producto: bool) -> int:
    restantes = [clave(o, con_producto) for o in obtenidas]
    n = 0
    for e in esperadas:
        k = clave(e, con_producto)
        if k in restantes:
            restantes.remove(k)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lote", action="store_true", help="usar procesar_lote (como batch.py)")
    args = ap.parse_args()

    mensajes = leer(DATA / "ejemplos_mensajes.csv")
    esperadas_todas = leer(DATA / "ejemplos_respuestas.csv")
    esperadas_por_id: dict[str, list[dict]] = {}
    for r in esperadas_todas:
        esperadas_por_id.setdefault(r["id_mensaje"].strip(), []).append(r)

    estado = nuevo_estado()
    t0 = time.time()
    if args.lote:
        res = procesar_lote([(m["id_mensaje"].strip(), m["mensaje"]) for m in mensajes], estado)
    else:
        res = {}
        for m in mensajes:
            mid = m["id_mensaje"].strip()
            res[mid] = procesar(mid, m["mensaje"], estado)

    total_esp = aciertos = aciertos_sin_prod = msj_ok = 0
    obtenidas_todas = []
    for m in mensajes:
        mid = m["id_mensaje"].strip()
        esp = esperadas_por_id.get(mid, [])
        obt = [a_fila_csv(f) for f in res[mid]["filas"]]
        obtenidas_todas += obt
        n = coincidencias(esp, obt, True)
        total_esp += len(esp)
        aciertos += n
        aciertos_sin_prod += coincidencias(esp, obt, False)
        ok = n == len(esp) == len(obt)
        msj_ok += ok
        print(f"{'OK  ' if ok else 'FALLA'} [{mid}] {m['mensaje'][:70]}")
        if not ok:
            for e in esp:
                print(f"        esperado: {e['tipo']}, {e['producto']}, {e['cantidad']}, {e['monto']}, {e['requiere_confirmacion']}")
            for o in obt:
                print(f"        obtenido: {o['tipo']}, {o['producto']}, {o['cantidad']}, {o['monto']}, {o['requiere_confirmacion']}")
        if res[mid]["pregunta"]:
            print(f"        pregunta: {res[mid]['pregunta']}")

    falsos_pos = len(obtenidas_todas) - aciertos
    print("\n" + "=" * 70)
    print(f"Mensajes perfectos:          {msj_ok}/{len(mensajes)}")
    print(f"Filas correctas (estricto):  {aciertos}/{total_esp} = {aciertos / max(total_esp, 1):.1%}")
    print(f"Filas correctas (sin mirar producto): {aciertos_sin_prod}/{total_esp}")
    print(f"Filas obtenidas de más o erradas: {falsos_pos}")
    flujo = flujo_caja(obtenidas_todas)["flujo"]
    print(f"Flujo calculado: {flujo:.2f}  |  esperado: {FLUJO_ESPERADO:.2f}  "
          f"|  {'OK' if abs(flujo - FLUJO_ESPERADO) < 0.01 else 'DIFERENTE'}")
    print(f"Flujo de ejemplos_respuestas.csv: {flujo_caja(esperadas_todas)['flujo']:.2f}")
    print(f"Tiempo: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
