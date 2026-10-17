# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Hackathon project "De la libreta a la caja" (Reto Mibanco): turns a Peruvian bodega owner's (Doña Rosa) free-text / voice notes into accounting transactions, asks back when data is missing, computes weekly cash flow, and estimates an affordable monthly loan installment. Code, identifiers, prompts and user-facing text are in Spanish — keep it that way. User-facing text must be simple and warm (the user is not digital-savvy).

## Working rules

- **Do not run git commands** (no init/add/commit/push). The user handles git.
- The repo is public: `GROQ_API_KEY` lives only in `.env` (gitignored). Never put keys in code, docs or chat.
- Each team member touches only their own files (see "Team split"). Changing the `procesar()` contract must be agreed first — it is the boundary between both halves.

## Setup & commands

```bash
pip install -r requirements.txt
cp .env.example .env          # set GROQ_API_KEY; optional MODELO_LLM, REASONING_EFFORT, MODELO_VOZ

python app.py                                   # Gradio UI at http://127.0.0.1:7860
python scripts/evaluar.py                       # accuracy vs data/ejemplos_respuestas.csv, one LLM call per message
python scripts/evaluar.py --lote                # same, batched (like batch.py)
python scripts/evaluar.py --mensajes ejemplos_mensajes_eval2.csv --respuestas ejemplos_respuestas_eval2.csv  # held-out 2nd evaluator (flow 1285.10)
python scripts/batch.py set_oculto.csv equipo   # writes resultados_equipo.csv (deliverable, must finish < 2 min for 20 msgs)
python scripts/batch.py set_oculto.csv equipo --por-mensaje
python -m src.caja data/ejemplos_respuestas.csv # cash-flow check, no API; must print flujo 788.90
```

There is no test suite or linter. `scripts/evaluar.py` is the regression check: it reports perfect messages, strict row accuracy, accuracy ignoring `producto`, extra/wrong rows, and whether total flow matches the flow of the given answers file (788.90 for the examples).

## Architecture

- `src/motor.py` — core engine. Contract: `procesar(id_mensaje: str, texto: str, estado: dict) -> {"filas": list[dict], "pregunta": str | None}`. Groq via the OpenAI SDK (`base_url=https://api.groq.com/openai/v1`, model from `MODELO_LLM`, default `openai/gpt-oss-120b`), JSON mode, temperature 0, retry/backoff on 429 (honors `retry-after`) and 5xx.
  - **Design rule: the LLM only extracts** (tipo, producto, cantidad, precio_unitario, descuento/descuento_pct, monto_total, cliente). Amounts are computed in Python (`calcular_monto`: cantidad × precio − descuento; falls back to monto_total). An amount is never invented nor set to 0: if missing, `monto=None`, `requiere_confirmacion="SI"`, and a question for the user is returned.
  - Missing `cantidad` defaults to 1 (expected output uses 1 for services, loose sales, payments).
  - `producto` is kept as the LLM returns it (no lowercasing) — expected answers have mixed case, e.g. "pago parcial don Pepe".
  - Shared `estado` dict (`nuevo_estado`) carries `transacciones`, `fiados` (client → pending debt), `fiados_producto` (client → product taken on credit) and `pendiente` (row awaiting an amount). `_aplicar` mutates it in message order. A `cobro_fiado` without amount resolves to the client's outstanding debt and takes the fiado's product (E03 → E12: "aceite primor", 11.00). A fiado without amount (E14) records the product but no debt.
  - Client names: `_clave_cliente` strips accents and courtesy words (la, señora, sra, don, doña, sr…); `_buscar_cliente` matches partial names ("Rosa" ↔ "rosa maria").
  - Few-shot examples come from `data/ejemplos_*.csv`; the message being processed is excluded from its own few-shot block (`_bloque_ejemplos(excluir)`, matched by normalized text, not id) so evaluation doesn't cheat.
  - `procesar` = one message per call (app). `procesar_lote` = blocks of 10 per call using `SISTEMA_LOTE`, falling back to `procesar` for any message the LLM skipped. `batch.py` uses `procesar_lote` to stay within Groq rate limits and the 2-minute limit.
  - `completar_pendiente` fills a pending row from a numeric follow-up reply (app chat flow) and updates fiados accordingly.
  - `a_fila_csv` formats rows for output; internal keys like `_cliente` are dropped.
- `src/motor_mock.py` — regex engine without LLM, same contract as `src/motor.py` (reuses `_aplicar`). `app.py` uses it when there is no `GROQ_API_KEY` or `MOCK_MOTOR=1`, and shows a "modo prueba" banner (voice does not work there). Only takes amounts written in digits, so "dos cervezas" never becomes an invented amount.
- `src/numeros.py` — `texto_a_monto` parses typed or spoken amounts ("12.50", "S/ 12,50", "seis cincuenta", "ciento ochenta"). Used by the app to read the answer to a pending question.
- `src/caja.py` — no API. `flujo_caja`: flow = ventas + cobros_fiado − gastos (fiados don't move cash). `estimar_cuota(filas, semanas=1)`: weekly flow × 4.3 × 0.30, excluding unconfirmed (`SI`) rows; returns 0 if flow ≤ 0. Text must always state it is only an estimate, never a promise or credit approval.
- `src/voz.py` — Whisper transcription on Groq (`MODELO_VOZ`, default `whisper-large-v3`, language `es`, `verbose_json`). `transcribir(ruta)` returns `""` for silence/noise: drops segments with `no_speech_prob ≥ 0.6` and Whisper hallucinations ("gracias por ver", "subtítulos"…, via `es_ruido`). Retries on 429/connection errors. `PISTA` is the vocabulary hint: add products or words Whisper gets wrong there.
- `app.py` — Gradio 6 Blocks UI (Chatbot uses messages format by default, no `type=` arg): chat + microphone, transactions table, cash summary, quota button, and upload of a previous week CSV (either raw `id_mensaje,dia,mensaje` → processed with `procesar_lote`, or already-processed rows with a `tipo` column). If a question is pending, the user's reply (digits or words, via `texto_a_monto`) completes the row; "no sé / no me acuerdo / después" leaves it as `SI`, marks it `_omitida` and stops asking; `siguiente_pregunta` then asks for the next row without amount, one at a time. The quota is shown as a range 25 %–30 % of monthly flow. Voice notes: the chat shows "🎤 *Te entendí:* …" (`responder(..., mostrar=)`) while the raw transcript is what gets processed; empty transcript → asks her to repeat closer to the phone. Previous week's pending fiados carry over.
- `motor_agente/` — alternative engine imported from `motor_mibanco.zip`; nothing in the app uses it. Different contract: `procesar(texto, estado, id_mensaje=None, modo="batch"|"chat")`. Reads `data/` from the repo root. Own venv in `motor_agente/.venv`; run from inside `motor_agente/`: `python test_mock.py` (no API, must print `IGUAL A LA CLAVE: True` and 788.9), `python evaluar.py [--lote]` (blind test on E21–E30), `python batch.py <csv> <equipo> [--por-mensaje]`. Excludes few-shot examples by id, so do not feed it the eval2 set (its ids E01–E40 collide with the examples).

## Business rules / tricky cases (from the kit)

Discounts (E06: 190 − 10 = 180), numbers in words (E07, E22 "seis cincuenta"), misspellings (E08 "bendi 6 guevos"), self-corrections — the corrected version wins (E11), missing amount → empty + SI (E09, E14, E20), refunds to customers → gasto (E18), personal/household expenses → no row (E26), "ventas sueltas"/end-of-day totals → venta with cantidad 1 (E05, E10…), cobro_fiado depending on an earlier fiado (E12, E21).

## Data contract

- Input set: `id_mensaje,dia,mensaje`.
- Output columns (from `data/plantilla_entrega.csv`, fallback `motor.COLUMNAS`): `id_mensaje,tipo,producto,cantidad,monto,requiere_confirmacion`. `tipo` ∈ {venta, gasto, fiado, cobro_fiado}; `monto` with dot decimal and 2 places, no "S/"; `cantidad` as integer when whole; `requiere_confirmacion` is `SI`/`NO`. Personal/household expenses produce no row.
- CSVs are read with `utf-8-sig` (files have a BOM).
- Held-out test set (written by the third teammate), in the repo root: `ejemplos_mensajes_eval2.csv` (40 messages) + `ejemplos_respuestas_eval2.csv` (42 rows, flow 1285.10). Same formats as the examples; its ids E01–E40 reuse the example ids but are different messages (`scripts/evaluar.py` excludes few-shot by text, so this is fine). Each fiado comes before its cobro (state is shared in order). New cases: personal expenses with "no es del negocio / de mi bolsillo" (E12, E24, E36), sale plus partial refund in the same message (E33 → venta + gasto), cobro with explicit amount equal to the debt (E38).

## Team split

- **Persona 1 — engine & deliverable:** `src/motor.py`, `src/caja.py`, `scripts/evaluar.py`, `scripts/batch.py`. Priorities: evaluator first, batch tested before minute 100, flow = 788.90.
- **Persona 2 — UX & demo:** `app.py`, `src/voz.py`, ask-back flow, quota explanation, demo.
- **Persona 3 (phone only):** held-out test messages with expected answers, 10–15 noisy voice notes, plays Doña Rosa in live tests, demo script, Groq account/keys (shared privately, never in the repo).
