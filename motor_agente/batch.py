"""python batch.py data/set_oculto.csv NOMBRE_EQUIPO  ->  resultados_NOMBRE_EQUIPO.csv"""
import csv
import sys
import time

from motor.procesar import flujo_caja, nuevo_estado, procesar

COLS = ["id_mensaje", "tipo", "producto", "cantidad", "monto", "requiere_confirmacion"]


def main(entrada, equipo, pausa=0.5):
    mensajes = list(csv.DictReader(open(entrada, encoding="utf-8-sig")))
    estado = nuevo_estado()
    for m in mensajes:  # en orden: es una sola semana
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
    for f in estado["filas"]:
        if f["requiere_confirmacion"] == "SI":
            print("  revisar:", f["id_mensaje"], f["producto"])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
