"""Motor: texto de Doña Rosa -> filas estructuradas.

procesar(texto, estado, id_mensaje=None, modo="batch") -> {"filas": [...], "pregunta": str | None}
"""
import csv
import json
import os
import time
import unicodedata
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"  # data/ de la raíz del repo

# Cualquier API compatible con OpenAI. Por defecto Groq.
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")  # llama-3.3-70b-versatile ya no existe en Groq

USO = {"llamadas": 0, "prompt_tokens": 0, "completion_tokens": 0}

TIPOS = {"venta", "gasto", "fiado", "cobro_fiado"}

# La clave de ejemplo no trae cliente; lo añadimos para que el few-shot lo enseñe.
CLIENTES_EJEMPLO = {"E03": "Rosa María", "E12": "Rosa María", "E14": "don Pepe", "E21": "don Pepe"}

SISTEMA = """Eres el motor de registro de una bodega en Lima. Conviertes mensajes de la dueña (Doña Rosa) en transacciones.
Devuelve SOLO un JSON: {"filas":[{"tipo","producto","cantidad","monto","requiere_confirmacion","cliente"}],"pregunta":null|"texto","actualiza":false}

REGLAS
- tipo: venta (cobrada al momento) | gasto (compras, servicios, ayudantes, devoluciones de dinero a clientes) | fiado (venta a crédito, no entra plata) | cobro_fiado (cliente paga algo que debía).
- monto = total en soles de la línea (cantidad x precio, con descuentos ya aplicados). Número con punto decimal, sin "S/".
- Una fila por producto u operación. Un mensaje puede dar varias filas o ninguna (gastos personales o de la casa NO son del negocio: filas = []).
- "ventas sueltas" / "salió X en abarrotes": una fila, producto "ventas sueltas" (o el que diga), cantidad 1, monto X.
- Si hay corrección ("no perdón, fueron 8"), registra solo la versión corregida.
- Entiende números en palabras ("seis cincuenta" = 6.50) y errores de ortografía ("bendi", "guevos"). Normaliza el nombre del producto en singular y con tildes.
- Si falta el monto y no se puede calcular con certeza: monto null, requiere_confirmacion "SI", y en "pregunta" una pregunta corta y simple en español peruano. NUNCA inventes un monto ni pongas 0.
- Si el monto sí está (o se calcula) requiere_confirmacion "NO".
- fiado y cobro_fiado: pon "cliente". Si el cobro no dice monto pero el cliente tiene un fiado abierto (te lo paso abajo), usa ese saldo.
- "actualiza": true SOLO si el mensaje responde a una pregunta pendiente (te la paso abajo) y completa esos datos.
- No apruebes ni prometas créditos."""


def _norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    for t in ("senora", "senor", "sra.", "sr.", "dona", "don", "sra", "sr"):
        s = s.replace(t, " ")
    return " ".join(s.split())


def _num(x):
    if x in (None, ""):
        return None
    f = float(x)
    return int(f) if f == int(f) else f


def cargar_ejemplos(excluir=()):
    """Lista de (id, mensaje, filas) leída de los CSV de ejemplo."""
    msgs = list(csv.DictReader(open(DATA / "ejemplos_mensajes.csv", encoding="utf-8-sig")))
    resp = list(csv.DictReader(open(DATA / "ejemplos_respuestas.csv", encoding="utf-8-sig")))
    por_id = {}
    for r in resp:
        por_id.setdefault(r["id_mensaje"], []).append(r)
    out = []
    for m in msgs:
        i = m["id_mensaje"]
        if i in excluir:
            continue
        filas = []
        for r in por_id.get(i, []):
            f = {"tipo": r["tipo"], "producto": r["producto"], "cantidad": _num(r["cantidad"]),
                 "monto": float(r["monto"]) if r["monto"] else None,
                 "requiere_confirmacion": r["requiere_confirmacion"]}
            if r["tipo"] in ("fiado", "cobro_fiado"):
                f["cliente"] = CLIENTES_EJEMPLO.get(i, "")
            filas.append(f)
        out.append((i, m["mensaje"], filas))
    return out


_cache_prompt = {}


def construir_sistema(excluir=()):
    key = tuple(sorted(excluir))
    if key not in _cache_prompt:
        ej = []
        for _, msg, filas in cargar_ejemplos(excluir):
            salida = {"filas": filas, "pregunta": None, "actualiza": False}
            ej.append(f"Mensaje: {msg}\nSalida: {json.dumps(salida, ensure_ascii=False, separators=(',', ':'))}")
        _cache_prompt[key] = SISTEMA + "\n\nEJEMPLOS\n" + "\n\n".join(ej)
    return _cache_prompt[key]


def _llm(sistema, usuario):
    """Una llamada al LLM. Devuelve dict. Reintenta si hay límite de uso."""
    from openai import OpenAI
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    ultimo = None
    for intento in range(5):
        try:
            r = client.chat.completions.create(
                model=MODEL, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": sistema}, {"role": "user", "content": usuario}])
            if r.usage:
                USO["llamadas"] += 1
                USO["prompt_tokens"] += r.usage.prompt_tokens
                USO["completion_tokens"] += r.usage.completion_tokens
            return json.loads(r.choices[0].message.content)
        except Exception as e:  # 429, JSON roto, red...
            codigo = getattr(e, "status_code", None)
            if codigo and 400 <= codigo < 500 and codigo != 429:
                raise  # modelo inexistente, key mala...: reintentar no sirve
            ultimo = e
            time.sleep(2 + 4 * intento)
    raise RuntimeError(f"LLM falló: {ultimo}")


def nuevo_estado():
    return {"fiados": [], "filas": [], "pendiente": None}


def _limpiar(f, id_mensaje):
    tipo = str(f.get("tipo", "")).strip().lower()
    if tipo not in TIPOS:
        return None
    monto = f.get("monto")
    try:
        monto = None if monto in (None, "") else round(float(monto), 2)
    except (TypeError, ValueError):
        monto = None
    cant = f.get("cantidad")
    try:
        cant = _num(cant) if cant not in (None, "") else 1
    except (TypeError, ValueError):
        cant = 1
    return {"id_mensaje": id_mensaje, "tipo": tipo, "producto": str(f.get("producto", "")).strip(),
            "cantidad": cant, "monto": monto,
            "requiere_confirmacion": "SI" if monto is None else "NO",
            "cliente": str(f.get("cliente") or "").strip()}


def _mismo_cliente(a, b):
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _saldos(estado, cliente):
    return [f for f in estado["fiados"] if _mismo_cliente(f["cliente"], cliente)
            and f["monto"] is not None and f["monto"] - f["cobrado"] > 0.005]


def _resolver_cobros(estado, filas):
    """Cobro sin monto + fiado conocido -> completa con el saldo (no inventa: usa lo registrado)."""
    for f in filas:
        if f["tipo"] == "cobro_fiado" and f["monto"] is None and f["cliente"]:
            abiertos = _saldos(estado, f["cliente"])
            if abiertos:
                a = abiertos[0]
                f["monto"] = round(a["monto"] - a["cobrado"], 2)
                f["requiere_confirmacion"] = "NO"
                if not f["producto"] or f["producto"] == "cobro":
                    f["producto"] = a["producto"]
                f["cantidad"] = a["cantidad"]


def _registrar(estado, filas):
    for f in filas:
        if f["tipo"] == "fiado":
            estado["fiados"].append({"cliente": f.get("cliente", ""), "producto": f["producto"],
                                     "cantidad": f["cantidad"], "monto": f["monto"], "cobrado": 0.0})
        elif f["tipo"] == "cobro_fiado" and f["monto"] is not None:
            resto = f["monto"]
            for a in _saldos(estado, f.get("cliente", "")):
                pago = min(resto, a["monto"] - a["cobrado"])
                a["cobrado"] += pago
                resto -= pago
                if resto <= 0.005:
                    break
    estado["filas"].extend(filas)


def aplicar_clave(estado, id_mensaje, filas_clave):
    """Mete filas conocidas (la clave) al estado sin llamar al LLM. Para evaluar sin gastar cuota."""
    filas = [_limpiar(f, id_mensaje) | {"cliente": f.get("cliente", "")} for f in filas_clave]
    _registrar(estado, [f for f in filas if f])


def _pregunta_por_defecto(filas):
    for f in filas:
        if f["monto"] is None:
            prod = f["producto"] or "eso"
            if f["tipo"] == "cobro_fiado":
                return f"¿Cuánto le cobró por {prod}?"
            return f"¿Cuánto fue el total de {prod}?"
    return None


def procesar(texto, estado, id_mensaje=None, modo="batch", excluir_ejemplos=()):
    id_mensaje = id_mensaje or f"M{len(estado['filas']) + 1:03d}"
    usuario = "Fiados abiertos: " + json.dumps(
        [{"cliente": a["cliente"], "producto": a["producto"], "cantidad": a["cantidad"],
          "saldo": None if a["monto"] is None else round(a["monto"] - a["cobrado"], 2)}
         for a in estado["fiados"]], ensure_ascii=False)
    pend = estado.get("pendiente") if modo == "chat" else None
    if pend:
        usuario += "\nPregunta pendiente (ya hecha a la usuaria): " + json.dumps(
            {"pregunta": pend["pregunta"], "filas": [{k: v for k, v in f.items() if k != "id_mensaje"} for f in pend["filas"]]},
            ensure_ascii=False)
    usuario += f"\nMensaje: {texto}"

    out = _llm(construir_sistema(excluir_ejemplos), usuario)
    filas = [x for x in (_limpiar(f, id_mensaje) for f in out.get("filas") or []) if x]

    if pend and out.get("actualiza"):
        # reemplaza las filas pendientes por las completadas
        viejo = pend["filas"][0]["id_mensaje"]
        estado["filas"] = [f for f in estado["filas"] if not (f["id_mensaje"] == viejo and f["monto"] is None)]
        for f in filas:
            f["id_mensaje"] = viejo
        estado["pendiente"] = None
        _resolver_cobros(estado, filas)
        _registrar(estado, filas)
        return {"filas": filas, "pregunta": None}

    _resolver_cobros(estado, filas)
    _registrar(estado, filas)
    faltan = [f for f in filas if f["monto"] is None]
    pregunta = None
    if faltan:
        pregunta = out.get("pregunta") or _pregunta_por_defecto(faltan)
        if modo == "chat":
            estado["pendiente"] = {"pregunta": pregunta, "filas": faltan}
    return {"filas": filas, "pregunta": pregunta}


SISTEMA_LOTE = ("\n\nLOTE: recibirás VARIOS mensajes en orden, cada uno con su id. Responde SOLO un JSON "
                '{"mensajes":[{"id":"...","filas":[...],"pregunta":null|"texto"}]} con un elemento por mensaje, '
                "en el mismo orden. Cada elemento sigue las mismas reglas que un mensaje suelto.")


def procesar_lote(mensajes, estado, tam=10, excluir_ejemplos=()):
    """[(id, texto), ...] -> {id: {"filas", "pregunta"}}. Una llamada al LLM por bloque de `tam`
    (el few-shot va una vez por bloque, no por mensaje: clave con el límite de tokens/minuto).
    Montos de cobros y fiados se resuelven en Python, mensaje por mensaje y en orden."""
    res = {}
    for i in range(0, len(mensajes), tam):
        bloque = mensajes[i:i + tam]
        usuario = "Fiados abiertos: " + json.dumps(
            [{"cliente": a["cliente"], "producto": a["producto"], "cantidad": a["cantidad"],
              "saldo": None if a["monto"] is None else round(a["monto"] - a["cobrado"], 2)}
             for a in estado["fiados"]], ensure_ascii=False)
        usuario += "\nMensajes:\n" + "\n".join(
            json.dumps({"id": mid, "mensaje": t}, ensure_ascii=False) for mid, t in bloque)
        out = _llm(construir_sistema(excluir_ejemplos) + SISTEMA_LOTE, usuario)
        por_id = {str(m.get("id")): m for m in out.get("mensajes") or []}
        for mid, texto in bloque:
            if mid not in por_id:  # el LLM se saltó el mensaje: va solo
                res[mid] = procesar(texto, estado, id_mensaje=mid, excluir_ejemplos=excluir_ejemplos)
                continue
            filas = [x for x in (_limpiar(f, mid) for f in por_id[mid].get("filas") or []) if x]
            _resolver_cobros(estado, filas)
            _registrar(estado, filas)
            faltan = [f for f in filas if f["monto"] is None]
            res[mid] = {"filas": filas,
                        "pregunta": (por_id[mid].get("pregunta") or _pregunta_por_defecto(faltan)) if faltan else None}
    return res


def flujo_caja(filas):
    """ventas + cobros de fiado - gastos. Los fiados y filas sin monto no cuentan."""
    t = 0.0
    for f in filas:
        m = f["monto"]
        if m is None:
            continue
        if f["tipo"] in ("venta", "cobro_fiado"):
            t += m
        elif f["tipo"] == "gasto":
            t -= m
    return round(t, 2)


def cuota_sugerida(filas, porcentaje=0.30):
    """Cuota mensual sostenible: flujo semanal x 4.3 x porcentaje. Solo estima, no aprueba crédito."""
    semanal = flujo_caja(filas)
    mensual = semanal * 4.3
    cuota = max(0.0, mensual * porcentaje)
    return {"flujo_semanal": semanal, "flujo_mensual": round(mensual, 2),
            "porcentaje": porcentaje, "cuota": round(cuota, 2)}
