"""Lee montos escritos o dictados: "12.50", "S/ 12,50", "seis cincuenta", "doce con veinte", "ciento ochenta"."""
from __future__ import annotations

import re
import unicodedata

UNIDADES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
    "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
DECENAS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
CENTENAS = {
    "cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300, "cuatrocientos": 400,
    "quinientos": 500, "seiscientos": 600, "setecientos": 700, "ochocientos": 800, "novecientos": 900,
}
RELLENO = {"sol", "soles", "s/", "s", "y", "con", "de", "a", "en", "total", "fue", "fueron", "son", "era", "eran",
           "nomas", "no", "mas", "como", "unos", "unas", "senora", "rosa"}
_DIGITOS = re.compile(r"(\d+(?:[.,]\d{1,2})?)")


def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _grupo(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Lee un número de 0 a 999 desde tokens[i]. Devuelve (valor, siguiente índice)."""
    valor, inicio = 0, i
    if i < len(tokens) and tokens[i] in CENTENAS:
        valor += CENTENAS[tokens[i]]
        i += 1
    if i < len(tokens) and tokens[i] in DECENAS:
        valor += DECENAS[tokens[i]]
        i += 1
        if i + 1 < len(tokens) and tokens[i] == "y" and tokens[i + 1] in UNIDADES and UNIDADES[tokens[i + 1]] < 10:
            valor += UNIDADES[tokens[i + 1]]
            i += 2
    elif i < len(tokens) and tokens[i] in UNIDADES:
        valor += UNIDADES[tokens[i]]
        i += 1
    return (valor, i) if i > inicio else (None, i)


def _numero(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Como _grupo pero admite miles ("mil doscientos")."""
    if i < len(tokens) and tokens[i] == "mil":
        valor, j = _grupo(tokens, i + 1)
        return 1000 + (valor or 0), j
    valor, j = _grupo(tokens, i)
    if valor is not None and j < len(tokens) and tokens[j] == "mil":
        resto, k = _grupo(tokens, j + 1)
        return valor * 1000 + (resto or 0), k
    return valor, j


def texto_a_monto(texto: str) -> float | None:
    """Primer monto que aparece en el texto, o None si no hay ninguno."""
    m = _DIGITOS.search(texto)
    if m:
        return round(float(m.group(1).replace(",", ".")), 2)
    tokens = re.findall(r"[a-zñ/]+", _norm(texto))
    for i in range(len(tokens)):
        entero, j = _numero(tokens, i)
        if entero is None:
            continue
        # céntimos: "seis cincuenta", "doce con veinte", "un sol cincuenta"
        while j < len(tokens) and tokens[j] in {"sol", "soles", "con"}:
            j += 1
        centimos, _ = _grupo(tokens, j)
        if centimos is not None and centimos < 100:
            return round(entero + centimos / 100, 2)
        return float(entero)
    return None
