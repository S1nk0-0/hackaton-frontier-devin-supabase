"""python batch.py ../data/set_oculto.csv NOMBRE_EQUIPO  ->  resultados_NOMBRE_EQUIPO.csv

Por defecto en lotes (pocas llamadas, entra en el límite de tokens/minuto de Groq).
--por-mensaje: una llamada por mensaje (más lento).
"""
import csv
import sys
import time

from motor.procesar import USO, flujo_caja, nuevo_estado, procesar, procesar_lote

COLS = ["id_mensaje", "tipo", "producto", "cantidad", "monto", "requiere_confirmacion"]


def main(entrada, equipo, pausa=0.5, lote=False):
    mensajes = list(csv.DictReader(open(entrada, encoding="utf-8-sig")))
    t0 = time.time()
    estado = nuevo_estado()
    if lote:
        procesar_lote([(m["id_mensaje"], m["mensaje"]) for m in mensajes], estado)
    for m in mensajes if not lote else []:  # en orden: es una sola semana
        try:
            procesar(m["mensaje"], estado, id_mensaje=m["id_mensaje"])
        except Exception as e:
            print(f"!! {m['id_mensaje']} falló: {e}")
        time.sleep(pausa)
    salida = f"resultados_{equipo}.csv"
    with open(salida, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for f in estado["filas"]:
            w.writerow([f["id_mensaje"], f["tipo"], f["producto"], f["cantidad"],
                        "" if f["monto"] is None else f"{f['monto']:.2f}", f["requiere_confirmacion"]])
    print(f"{salida}: {len(estado['filas'])} filas | flujo de caja {flujo_caja(estado['filas'])}")
    print(f"tiempo {time.time() - t0:.1f}s | uso {USO}")
    for f in estado["filas"]:
        if f["requiere_confirmacion"] == "SI":
            print("  revisar:", f["id_mensaje"], f["producto"])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], lote="--por-mensaje" not in sys.argv)
