"""Servidor HTTP do Rápido TTS com a API de fala da OpenAI/Speaches.

POST /v1/audio/speech  {"input": texto, "speed": 1.0, "response_format": "wav"}  -> WAV 48 kHz, 16 bits, mono
GET  /health           -> "OK" quando o modelo já está carregado
`model` e `voice` são aceitos e ignorados: o Rápido tem uma voz só.
"""

import io
import os
import threading
import time
import wave

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from rapido import Config, Rapido

app = FastAPI(title="rapido-tts")
_modelo: Rapido | None = None
_trava = threading.Lock()  # um modelo na GPU: uma síntese por vez, em ordem


class Pedido(BaseModel):
    input: str = Field(min_length=1, max_length=4000)
    model: str | None = None
    voice: str | None = None
    response_format: str = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@app.on_event("startup")
def carregar() -> None:
    global _modelo
    t0 = time.perf_counter()
    _modelo = Rapido(Config(device=os.environ.get("RAPIDO_DEVICE", "cuda"), seed=0))
    _modelo.synthesize("Pronto.")  # aquece a GPU; a primeira síntese é mais lenta
    print(f"rapido-tts: modelo carregado em {time.perf_counter() - t0:.1f}s", flush=True)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    if _modelo is None:
        raise HTTPException(503, "modelo ainda carregando")
    return "OK"


@app.post("/v1/audio/speech")
def falar(pedido: Pedido) -> Response:
    if _modelo is None:
        raise HTTPException(503, "modelo ainda carregando")
    if pedido.response_format != "wav":
        raise HTTPException(400, "só response_format=wav")
    t0 = time.perf_counter()
    with _trava:
        resultado = _modelo.synthesize(pedido.input, speed=pedido.speed)
    audio = resultado.output["audio"]
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(resultado.output["sample_rate"])
        w.writeframes(pcm.tobytes())
    dur = len(pcm) / resultado.output["sample_rate"]
    print(f"rapido-tts: {len(pedido.input)} caracteres -> {dur:.1f}s de áudio em {time.perf_counter() - t0:.2f}s", flush=True)
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
