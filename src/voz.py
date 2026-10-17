"""Transcripción de notas de voz con Whisper (Groq).

transcribir(ruta_audio) -> str   ("" si el audio estaba vacío o era solo ruido)
"""
from __future__ import annotations

import os
import re
import time
import unicodedata

from dotenv import load_dotenv

load_dotenv()

MODELO_VOZ = os.getenv("MODELO_VOZ", "whisper-large-v3")
# Pista de vocabulario: si Whisper escribe mal un producto o cifra, añádelo aquí.
PISTA = (
    "Bodega en Perú. Vendí, compré, pagué, fié, me pagó, fiado, soles, céntimos, "
    "gaseosa, arroz, azúcar, aceite primor, leche gloria, pan, huevos, atún, menestras, balón de gas."
)
# Frases que Whisper "inventa" cuando el audio está vacío o es solo ruido.
ALUCINACIONES = re.compile(
    r"gracias por ver|subtitul|suscrib|amara\.org|transcripcion|^(gracias|musica|aplausos|\W*)\.?$"
)
MAX_NO_SPEECH = 0.6  # segmentos con más probabilidad de "no hay voz" se descartan


def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower().strip())
    return "".join(c for c in texto if not unicodedata.combining(c))


def es_ruido(texto: str) -> bool:
    t = _norm(texto)
    return len(t) < 3 or bool(ALUCINACIONES.search(t))


def _campo(seg, nombre):
    return seg.get(nombre) if isinstance(seg, dict) else getattr(seg, nombre, None)


def _texto_limpio(resp) -> str:
    """Une los segmentos con voz real; si la API no da segmentos, usa el texto completo."""
    segmentos = getattr(resp, "segments", None) or []
    if not segmentos:
        return (resp.text or "").strip()
    partes = [
        str(_campo(s, "text") or "").strip()
        for s in segmentos
        if (_campo(s, "no_speech_prob") or 0) < MAX_NO_SPEECH
    ]
    return " ".join(p for p in partes if p and not es_ruido(p)).strip()


def transcribir(ruta_audio: str, max_intentos: int = 4) -> str:
    from openai import APIConnectionError, OpenAI, RateLimitError

    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Falta GROQ_API_KEY en .env")
    cliente = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
    for intento in range(max_intentos):
        try:
            with open(ruta_audio, "rb") as f:
                resp = cliente.audio.transcriptions.create(
                    model=MODELO_VOZ,
                    file=f,
                    language="es",
                    temperature=0,
                    prompt=PISTA,
                    response_format="verbose_json",
                )
            texto = _texto_limpio(resp)
            return "" if es_ruido(texto) else texto
        except (RateLimitError, APIConnectionError):
            if intento == max_intentos - 1:
                raise
            time.sleep(min(2 ** intento, 8))
    return ""
