"""Transcripción de notas de voz con Whisper (Groq)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

MODELO_VOZ = os.getenv("MODELO_VOZ", "whisper-large-v3")


def transcribir(ruta_audio: str) -> str:
    from openai import OpenAI

    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Falta GROQ_API_KEY en .env")
    cliente = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
    with open(ruta_audio, "rb") as f:
        resp = cliente.audio.transcriptions.create(
            model=MODELO_VOZ,
            file=f,
            language="es",
            temperature=0,
            prompt="Bodega en Perú. Soles, fiado, gaseosa, arroz, azúcar, pan, menestras.",
        )
    return resp.text.strip()
