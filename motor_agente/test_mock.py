"""Prueba sin API: un LLM falso que devuelve la clave (con E12 sin monto) para validar estado, CSV y flujo."""
import csv, json, sys
import motor.procesar as P

ej = {i: (m, f) for i, m, f in P.cargar_ejemplos()}
por_msg = {m: f for m, f in ej.values()}

def falso(sistema, usuario):
    msg = usuario.split("Mensaje: ", 1)[1]
    filas = [dict(f) for f in por_msg[msg]]
    for f in filas:
        if msg.startswith("la señora Rosa María me pagó"):
            f["monto"] = None; f["requiere_confirmacion"] = "SI"   # el motor debe recordarlo
    return {"filas": filas, "pregunta": None, "actualiza": False}

P._llm = falso
import batch
batch.main(P.DATA / "ejemplos_mensajes.csv", "MOCK", pausa=0)
a = list(csv.DictReader(open("resultados_MOCK.csv"))); b = list(csv.DictReader(open(P.DATA / "ejemplos_respuestas.csv", encoding="utf-8-sig")))
def norm(r): return (r["id_mensaje"], r["tipo"], r["monto"], r["requiere_confirmacion"])
print("IGUAL A LA CLAVE:", sorted(map(norm, a)) == sorted(map(norm, b)))
print(open("resultados_MOCK.csv").readline().strip())
