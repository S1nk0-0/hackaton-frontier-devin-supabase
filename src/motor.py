"""Motor: convierte mensajes de Doña Rosa en transacciones.

Contrato principal:
    procesar(id_mensaje, texto, estado) -> {"filas": list[dict], "pregunta": str | None}

El LLM (Groq, API compatible con OpenAI) solo EXTRAE datos: tipo, producto,
cantidad, precio unitario, descuento y, si la señora lo dice directo, el total.
El monto se calcula aquí en Python. Nunca se inventa un monto: si falta,
queda vacío, requiere_confirmacion=SI y se devuelve una pregunta.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
COLUMNAS = ["id_mensaje", "tipo", "producto", "cantidad", "monto", "requiere_confirmacion"]
TIPOS = {"venta", "gasto", "fiado", "cobro_fiado"}

MODELO = os.getenv("MODELO_LLM", "openai/gpt-oss-120b")
MODELO_RESPALDO = os.getenv("MODELO_LLM_RESPALDO", "llama-3.3-70b-versatile")
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "medium")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
TIMEOUT_S = 45
MAX_ESPERA_429 = 30  # si un 429 pide esperar más, se pasa a la siguiente opción

_clientes: dict[str, object] = {}
_opcion = 0  # índice en _opciones(); no se vuelve atrás a una opción agotada
LLAMADAS: list[dict] = []  # una entrada por llamada al LLM: qué key/modelo se usó y qué falló


# --------------------------------------------------------------------------- estado

def nuevo_estado() -> dict:
    """Estado compartido entre mensajes."""
    return {
        "transacciones": [],  # filas ya registradas (formato de salida)
        "fiados": {},         # cliente -> monto pendiente
        "fiados_producto": {},  # cliente -> producto que se llevó fiado
        "pendiente": None,    # {"fila": dict, "pregunta": str} si falta un dato
    }


# --------------------------------------------------------------------------- LLM

def _opciones() -> list[tuple[str, str, str]]:
    """(nombre_key, key, modelo) en orden: key 1 → key 2 → respaldo con key 2 → respaldo con key 1."""
    k1, k2 = os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEY_2")
    if not k1:
        raise RuntimeError("Falta GROQ_API_KEY. Copia .env.example a .env y pon tu key.")
    orden = [("key 1", k1, MODELO), ("key 2", k2, MODELO), ("key 2", k2, MODELO_RESPALDO), ("key 1", k1, MODELO_RESPALDO)]
    vistas, out = set(), []
    for nombre, key, modelo in orden:
        if key and (key, modelo) not in vistas:
            vistas.add((key, modelo))
            out.append((nombre, key, modelo))
    return out


def _cliente_para(key: str):
    if key not in _clientes:
        from openai import OpenAI

        _clientes[key] = OpenAI(api_key=key, base_url=GROQ_BASE_URL, timeout=TIMEOUT_S, max_retries=0)
    return _clientes[key]


def _llamar_llm(mensajes: list[dict], max_intentos: int = 6, etiqueta: str = "") -> dict:
    """Llama al LLM pidiendo JSON. Reintenta con backoff ante 429 / 5xx / timeout.

    Si un 429 pide esperar más de MAX_ESPERA_429 s (p. ej. límite diario agotado) o el modelo
    no existe para esa key (404), pasa a la siguiente opción de _opciones() sin esperar.
    """
    global _opcion
    from openai import APIConnectionError, APIStatusError, NotFoundError, RateLimitError

    opciones = _opciones()
    registro = {"etiqueta": etiqueta, "uso": None, "fallos": []}
    LLAMADAS.append(registro)
    ultimo_error = None
    for intento in range(max_intentos):
        nombre, key, modelo = opciones[min(_opcion, len(opciones) - 1)]
        extra = {"reasoning_effort": REASONING_EFFORT} if "gpt-oss" in modelo and REASONING_EFFORT else {}
        try:
            resp = _cliente_para(key).chat.completions.create(
                model=modelo,
                messages=mensajes,
                temperature=0,
                response_format={"type": "json_object"},
                **extra,
            )
            salida = json.loads(resp.choices[0].message.content)
            registro["uso"] = f"{nombre} / {modelo}"
            return salida
        except RateLimitError as e:
            ultimo_error = e
            espera = _retry_after(e) or min(2 ** intento, 30)
        except (APIConnectionError, json.JSONDecodeError) as e:  # incluye timeout
            ultimo_error = e
            espera = min(2 ** intento, 10)
        except APIStatusError as e:
            if e.status_code < 500 and not isinstance(e, NotFoundError):
                raise
            ultimo_error = e
            espera = 0 if isinstance(e, NotFoundError) else min(2 ** intento, 10)
        registro["fallos"].append(f"{nombre} / {modelo}: {type(ultimo_error).__name__}")
        cambiar = isinstance(ultimo_error, NotFoundError) or (
            isinstance(ultimo_error, RateLimitError) and espera > MAX_ESPERA_429)
        if cambiar:
            if _opcion >= len(opciones) - 1:  # no queda respaldo: mejor fallar ya que esperar minutos
                raise RuntimeError(f"Todas las keys/modelos agotados; el último falló con: {ultimo_error}")
            _opcion += 1
            sig = opciones[_opcion]
            print(f"  [motor] {nombre} / {modelo} no disponible ({type(ultimo_error).__name__}); paso a {sig[0]} / {sig[2]}")
            continue
        print(f"  [motor] reintento {intento + 1}/{max_intentos} en {espera:.1f}s ({type(ultimo_error).__name__})")
        time.sleep(espera)
    raise RuntimeError(f"El LLM falló tras {max_intentos} intentos: {ultimo_error}")


def _retry_after(error) -> float | None:
    try:
        valor = error.response.headers.get("retry-after")
        return float(valor) + 0.5 if valor else None
    except Exception:
        return None


# --------------------------------------------------------------------------- few-shot

def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower().strip())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _leer_csv(ruta: Path) -> list[dict]:
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def cargar_ejemplos() -> list[dict]:
    """[{id_mensaje, mensaje, filas:[...]}] a partir de data/ejemplos_*.csv (vacío si no existen)."""
    ruta_m, ruta_r = DATA / "ejemplos_mensajes.csv", DATA / "ejemplos_respuestas.csv"
    if not (ruta_m.exists() and ruta_r.exists()):
        return []
    por_id: dict[str, list[dict]] = {}
    for r in _leer_csv(ruta_r):
        por_id.setdefault(r["id_mensaje"].strip(), []).append(r)
    ejemplos = []
    for m in _leer_csv(ruta_m):
        mid = m["id_mensaje"].strip()
        filas = [
            {
                "tipo": r["tipo"],
                "producto": r["producto"],
                "cantidad": _a_numero(r.get("cantidad")),
                "monto_total": _a_numero(r.get("monto")),
            }
            for r in por_id.get(mid, [])
        ]
        ejemplos.append({"id_mensaje": mid, "mensaje": m["mensaje"], "filas": filas})
    return ejemplos


_EJEMPLOS: list[dict] | None = None


def _ejemplos() -> list[dict]:
    global _EJEMPLOS
    if _EJEMPLOS is None:
        _EJEMPLOS = cargar_ejemplos()
    return _EJEMPLOS


SISTEMA = """Eres el asistente contable de Doña Rosa, dueña de una bodega en Perú.
Conviertes sus mensajes (texto o notas de voz transcritas) en transacciones del negocio.

TIPOS:
- venta: le vendió algo a un cliente (entra dinero).
- gasto: compras de mercadería, servicios (luz, agua, alquiler), pago a ayudantes, devoluciones de dinero a clientes (sale dinero).
- fiado: un cliente se llevó algo sin pagar (no entra dinero todavía). Indica el nombre del cliente en "cliente".
- cobro_fiado: un cliente le pagó lo que debía. Indica el nombre del cliente en "cliente".

REGLAS:
1. Un mensaje puede tener varias transacciones o ninguna. Los gastos PERSONALES o del hogar (no del negocio) NO se registran.
2. Si ella se corrige ("no perdón, fueron 8"), usa SOLO la versión corregida.
3. Para cada transacción extrae: cantidad, precio_unitario, descuento (en soles) o descuento_pct (porcentaje), y monto_total solo si ella dice el total directamente. NO hagas cálculos: el sistema los hace.
   Un precio puede venir como "a 11", "está 11 soles", "cuesta 11", "sale 11", "11 soles" después del producto: eso es precio_unitario (o monto_total si es el total), no falta el monto.
4. Si NO hay forma de saber el monto (ni total ni precio), deja precio_unitario y monto_total en null. NUNCA inventes un monto ni pongas 0.
5. Si es un cobro de fiado y no dice cuánto, deja el monto en null y pon el nombre del cliente: el sistema busca cuánto debía.
6. Usa nombres de producto cortos y en minúscula, con el mismo estilo de los ejemplos.
7. Si falta el monto, escribe en "pregunta" una pregunta corta, cálida y sencilla para Doña Rosa (tutéala con cariño, sin tecnicismos).

FORMATO DE RESPUESTA (solo JSON):
{"filas": [{"tipo": "...", "producto": "...", "cantidad": numero|null, "precio_unitario": numero|null,
  "descuento": numero|null, "descuento_pct": numero|null, "monto_total": numero|null, "cliente": "..."|null}],
 "pregunta": "..."|null}

En los ejemplos solo se muestra el total esperado (monto_total); tú desglosa precio_unitario y descuento cuando el mensaje los dé."""

SISTEMA_LOTE = SISTEMA.replace(
    "FORMATO DE RESPUESTA (solo JSON):",
    "Recibirás VARIOS mensajes en orden. Responde uno por cada mensaje, en el mismo orden y con su id_mensaje.\n\n"
    'FORMATO DE RESPUESTA (solo JSON): {"mensajes": [{"id_mensaje": "...", "filas": [...], "pregunta": ...}]} donde cada elemento tiene:',
)


def _bloque_ejemplos(excluir: set[str]) -> str:
    lineas = []
    for ej in _ejemplos():
        if _norm(ej["mensaje"]) in excluir:  # no "copiar" la respuesta del mismo mensaje
            continue
        lineas.append(f'Mensaje: {ej["mensaje"]}\nRespuesta: {json.dumps({"filas": ej["filas"]}, ensure_ascii=False)}')
    return "EJEMPLOS:\n\n" + "\n\n".join(lineas) if lineas else ""


def _contexto(estado: dict) -> str:
    partes = []
    if estado["fiados"]:
        partes.append("Fiados pendientes por cliente: " + json.dumps(
            {c: {"monto": m, "producto": estado.get("fiados_producto", {}).get(c)} for c, m in estado["fiados"].items()},
            ensure_ascii=False))
    ultimas = estado["transacciones"][-5:]
    if ultimas:
        partes.append("Últimas transacciones registradas: " + json.dumps(ultimas, ensure_ascii=False))
    return "\n".join(partes)


# --------------------------------------------------------------------------- cálculo en Python

def _a_numero(valor) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().lower().replace("s/", "").replace(",", ".").strip()
    if not s or s in {"null", "none", "nan"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def calcular_monto(f: dict) -> float | None:
    """monto = cantidad × precio − descuento. Si solo hay total, se usa el total."""
    precio = _a_numero(f.get("precio_unitario"))
    total = _a_numero(f.get("monto_total"))
    if precio is not None:
        cantidad = _a_numero(f.get("cantidad"))
        bruto = (cantidad if cantidad is not None else 1) * precio
        pct = _a_numero(f.get("descuento_pct"))
        if pct:
            bruto *= 1 - pct / 100
        bruto -= _a_numero(f.get("descuento")) or 0
        return round(bruto, 2)
    if total is not None:
        return round(total, 2)
    return None


_TRATAMIENTOS = {"la", "el", "senora", "sra", "sra.", "senor", "sr", "sr.", "don", "dona", "seno", "vecina", "vecino"}


def _clave_cliente(nombre) -> str:
    return " ".join(p for p in _norm(str(nombre or "")).split() if p not in _TRATAMIENTOS)


def _buscar_cliente(clave: str, estado: dict) -> str | None:
    """Encuentra el cliente en fiados aunque el nombre venga incompleto ("Rosa" vs "rosa maria")."""
    if clave in estado["fiados"]:
        return clave
    for c in estado["fiados"]:
        if clave and (clave in c or c in clave):
            return c
    return None


def _aplicar(id_mensaje: str, salida_llm: dict, estado: dict) -> dict:
    """Convierte la salida del LLM en filas finales y actualiza el estado."""
    filas, faltantes = [], []
    for f in salida_llm.get("filas") or []:
        tipo = str(f.get("tipo", "")).strip().lower()
        if tipo not in TIPOS:
            continue
        monto = calcular_monto(f)
        producto = str(f.get("producto") or "").strip()
        cliente = _clave_cliente(f.get("cliente") or f.get("producto"))
        if tipo == "cobro_fiado":
            cliente = _buscar_cliente(cliente, estado) or cliente

        if tipo == "cobro_fiado" and monto is None and cliente in estado["fiados"]:
            monto = estado["fiados"][cliente]  # cobro sin monto → lo que debía
            producto = estado["fiados_producto"].get(cliente) or producto  # E12: "aceite primor"
        if tipo == "fiado":
            estado["fiados_producto"][cliente] = producto
            if monto is not None:
                estado["fiados"][cliente] = round(estado["fiados"].get(cliente, 0) + monto, 2)
        if tipo == "cobro_fiado" and monto is not None and cliente in estado["fiados"]:
            restante = round(estado["fiados"][cliente] - monto, 2)
            if restante > 0:
                estado["fiados"][cliente] = restante
            else:
                estado["fiados"].pop(cliente)

        fila = {
            "id_mensaje": id_mensaje,
            "tipo": tipo,
            "producto": producto,
            "cantidad": _a_numero(f.get("cantidad")) or 1.0,  # sin cantidad → 1 (agua, luz, ventas sueltas)
            "monto": monto,
            "requiere_confirmacion": "SI" if monto is None else "NO",
        }
        if tipo in {"fiado", "cobro_fiado"}:
            fila["_cliente"] = cliente
        filas.append(fila)
        if monto is None:
            faltantes.append(fila)

    estado["transacciones"].extend(filas)
    pregunta = None
    if faltantes:
        pregunta = salida_llm.get("pregunta") or (
            f"Doña Rosa, ¿cuánto fue en total lo de {faltantes[0]['producto'] or 'esa operación'}?"
        )
        estado["pendiente"] = {"fila": faltantes[0], "pregunta": pregunta}
    return {"filas": filas, "pregunta": pregunta}


# --------------------------------------------------------------------------- API pública

def procesar(id_mensaje: str, texto: str, estado: dict) -> dict:
    """Procesa UN mensaje. Devuelve {"filas": [...], "pregunta": str | None}."""
    sistema = SISTEMA + "\n\n" + _bloque_ejemplos({_norm(texto)})
    usuario = (_contexto(estado) + "\n\n" if _contexto(estado) else "") + f"Mensaje: {texto}"
    salida = _llamar_llm([{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
                         etiqueta=f"mensaje {id_mensaje}")
    return _aplicar(str(id_mensaje), salida, estado)


def procesar_lote(mensajes: list[tuple[str, str]], estado: dict, tam: int = 10) -> dict[str, dict]:
    """Procesa muchos mensajes con pocas llamadas (para batch.py y el set oculto).

    El LLM recibe los mensajes en bloques de `tam`; el cálculo de montos y fiados
    se hace en Python en orden, con el mismo estado compartido.
    """
    resultados: dict[str, dict] = {}
    for i in range(0, len(mensajes), tam):
        bloque = mensajes[i:i + tam]
        sistema = SISTEMA_LOTE + "\n\n" + _bloque_ejemplos({_norm(t) for _, t in bloque})
        usuario = (_contexto(estado) + "\n\n" if _contexto(estado) else "") + "MENSAJES:\n" + "\n".join(
            json.dumps({"id_mensaje": mid, "mensaje": t}, ensure_ascii=False) for mid, t in bloque
        )
        salida = _llamar_llm([{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
                             etiqueta=f"bloque {bloque[0][0]}–{bloque[-1][0]}")
        por_id = {str(m.get("id_mensaje")): m for m in salida.get("mensajes") or []}
        for mid, texto in bloque:
            if str(mid) in por_id:
                resultados[mid] = _aplicar(str(mid), por_id[str(mid)], estado)
            else:  # el LLM se saltó un mensaje: lo procesamos solo
                resultados[mid] = procesar(mid, texto, estado)
    return resultados


_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def completar_pendiente(estado: dict, texto: str) -> dict | None:
    """Si hay una fila esperando el monto y `texto` trae un número, la completa."""
    pendiente = estado.get("pendiente")
    if not pendiente:
        return None
    m = _NUM.search(texto)
    if not m:
        return None
    fila = pendiente["fila"]
    fila["monto"] = round(float(m.group(1).replace(",", ".")), 2)
    fila["requiere_confirmacion"] = "NO"
    c = fila.get("_cliente", "")
    if fila["tipo"] == "fiado":
        estado["fiados"][c] = round(estado["fiados"].get(c, 0) + fila["monto"], 2)
    elif fila["tipo"] == "cobro_fiado" and c in estado["fiados"]:
        restante = round(estado["fiados"][c] - fila["monto"], 2)
        if restante > 0:
            estado["fiados"][c] = restante
        else:
            estado["fiados"].pop(c)
    estado["pendiente"] = None
    return fila


# --------------------------------------------------------------------------- formato de salida

def _fmt_num(x) -> str:
    if x is None or x == "":
        return ""
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:g}"


def a_fila_csv(fila: dict) -> dict:
    """Fila lista para escribir en el CSV de entrega (monto con punto decimal, sin S/)."""
    return {
        "id_mensaje": fila["id_mensaje"],
        "tipo": fila["tipo"],
        "producto": fila["producto"],
        "cantidad": _fmt_num(fila.get("cantidad")),
        "monto": "" if fila.get("monto") is None else f"{float(fila['monto']):.2f}",
        "requiere_confirmacion": fila["requiere_confirmacion"],
    }
