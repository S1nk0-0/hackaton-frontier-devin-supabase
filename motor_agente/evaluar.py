"""Evalúa el motor contra ejemplos_respuestas.csv.

python evaluar.py            # prueba ciega: los últimos 10 ejemplos NO van en el prompt
python evaluar.py --ultimos 5
python evaluar.py --ultimos 0   # todos en el prompt (solo para verificar que no se rompe)
Los mensajes que sí van en el prompt se meten al estado con la clave (sin gastar llamadas al LLM).
"""
import argparse
import csv
import time
from collections import Counter

from motor.procesar import DATA, aplicar_clave, cargar_ejemplos, flujo_caja, nuevo_estado, procesar


def clave_fila(f):
    m = f["monto"]
    return (f["tipo"], None if m is None else round(float(m), 2), f["requiere_confirmacion"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ultimos", type=int, default=10)
    ap.add_argument("--pausa", type=float, default=1.0)
    a = ap.parse_args()

    ejemplos = cargar_ejemplos()
    ids = [i for i, _, _ in ejemplos]
    ciegos = set(ids[-a.ultimos:]) if a.ultimos else set(ids)

    estado = nuevo_estado()
    ok = falt = extra = 0
    esperadas_ciegas = []
    for i, msg, filas_clave in ejemplos:
        if i not in ciegos:
            aplicar_clave(estado, i, filas_clave)
            continue
        r = procesar(msg, estado, id_mensaje=i, excluir_ejemplos=ciegos if a.ultimos else ())
        time.sleep(a.pausa)
        esp = Counter(clave_fila(f) for f in filas_clave)
        got = Counter(clave_fila(f) for f in r["filas"])
        buenas = sum((esp & got).values())
        faltan, sobran = esp - got, got - esp
        ok += buenas
        falt += sum(faltan.values())
        extra += sum(sobran.values())
        esperadas_ciegas += filas_clave
        if faltan or sobran:
            print(f"✗ {i}: {msg}\n    esperado de menos: {dict(faltan)}\n    de más:           {dict(sobran)}")

    total = sum(1 for _ in esperadas_ciegas)
    print(f"\nFilas correctas {ok}/{total} | faltan {falt} | de más {extra}")
    print(f"Flujo de caja (todo el estado): {flujo_caja(estado['filas'])}  (clave completa: 788.9)")


if __name__ == "__main__":
    main()
