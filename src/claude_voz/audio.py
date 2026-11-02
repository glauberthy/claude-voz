"""Gravação (arecord), transcrição e síntese (Speaches) e reprodução (aplay)."""

from __future__ import annotations

import asyncio
import signal
import time
from pathlib import Path

import httpx


class Speaches:
    """Cliente HTTP do Speaches: /v1/audio/transcriptions e /v1/audio/speech."""

    def __init__(self, url: str, voz: str, modelo_tts: str, modelo_stt: str, velocidade: float, prompt_stt: str,
                 url_tts: str | None = None):
        self.url = url.rstrip("/")                      # transcrição (Whisper)
        self.url_tts = (url_tts or url).rstrip("/")     # voz: o mesmo Speaches, ou outro servidor com a mesma API
        self.voz = voz
        self.modelo_tts = modelo_tts
        self.modelo_stt = modelo_stt
        self.velocidade = velocidade
        self.prompt_stt = prompt_stt
        self._http = httpx.AsyncClient(base_url=self.url, timeout=httpx.Timeout(60.0, connect=3.0))
        self._http_tts = self._http if self.url_tts == self.url else httpx.AsyncClient(base_url=self.url_tts, timeout=httpx.Timeout(60.0, connect=3.0))

    async def saudavel(self) -> bool:
        """Transcrição no ar?"""
        return await self._saudavel(self._http)

    async def saudavel_tts(self) -> bool:
        """Voz no ar?"""
        return await self._saudavel(self._http_tts)

    @staticmethod
    async def _saudavel(http: httpx.AsyncClient) -> bool:
        try:
            r = await http.get("/health", timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def transcrever(self, wav: Path) -> tuple[str, float]:
        """Devolve (texto, segundos gastos)."""
        dados = {"model": self.modelo_stt, "language": "pt", "response_format": "json"}
        if self.prompt_stt:
            dados["prompt"] = self.prompt_stt
        t0 = time.perf_counter()
        with wav.open("rb") as f:
            r = await self._http.post("/v1/audio/transcriptions", data=dados, files={"file": (wav.name, f, "audio/wav")})
        r.raise_for_status()
        return r.json().get("text", "").strip(), time.perf_counter() - t0

    async def sintetizar(self, frase: str) -> bytes:
        corpo = {
            "input": frase,
            "model": self.modelo_tts,
            "voice": self.voz,
            "response_format": "wav",
            "speed": self.velocidade,
        }
        r = await self._http_tts.post("/v1/audio/speech", json=corpo)
        r.raise_for_status()
        return r.content

    async def fechar(self) -> None:
        await self._http.aclose()
        if self._http_tts is not self._http:
            await self._http_tts.aclose()


class Gravador:
    """Grava do microfone padrão com arecord (16 kHz, mono, 16 bits), até ser parado."""

    def __init__(self, destino: Path):
        self.destino = destino
        self._proc: asyncio.subprocess.Process | None = None

    async def iniciar(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", str(self.destino),
            stdin=asyncio.subprocess.DEVNULL,
        )

    async def parar(self) -> Path:
        """SIGINT faz o arecord fechar o WAV corretamente (grava o cabeçalho)."""
        if self._proc and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self._proc.wait(), 3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        return self.destino

    async def cancelar(self) -> None:
        await self.parar()
        self.destino.unlink(missing_ok=True)


class Reprodutor:
    """Toca WAVs em ordem com aplay. Tem fila de texto -> fila de áudio -> alto-falante.

    Dois workers:
      - `_sintetizar_worker`: tira frase da fila de texto, chama o Speaches, põe o WAV na fila de áudio.
      - `_tocar_worker`: tira WAV da fila de áudio e toca com aplay, um por vez.
    Nenhum dos dois bloqueia quem recebe mensagens do SDK: `falar()` só enfileira.
    """

    def __init__(self, speaches: Speaches, ao_comecar_primeiro_audio=None):
        self._speaches = speaches
        self._fila_texto: asyncio.Queue[str | None] = asyncio.Queue()
        self._fila_audio: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=8)
        self._aplay: asyncio.subprocess.Process | None = None
        self._tarefas: list[asyncio.Task] = []
        self._geracao = 0  # muda a cada interrupção: descarta áudio de resposta antiga
        self._ao_comecar_primeiro_audio = ao_comecar_primeiro_audio
        self._primeiro_audio_pendente = False
        self.erro_tts: str | None = None

    def iniciar(self) -> None:
        self._tarefas = [
            asyncio.create_task(self._sintetizar_worker(), name="tts"),
            asyncio.create_task(self._tocar_worker(), name="aplay"),
        ]

    def nova_resposta(self) -> None:
        self._primeiro_audio_pendente = True

    def falar(self, frase: str) -> None:
        self._fila_texto.put_nowait(frase)

    def interromper(self) -> None:
        """Esvazia as filas e mata o aplay atual. Síncrono: roda inteiro antes de qualquer worker continuar."""
        self._geracao += 1
        for fila in (self._fila_texto, self._fila_audio):
            while not fila.empty():
                try:
                    fila.get_nowait()
                    fila.task_done()
                except asyncio.QueueEmpty:
                    break
        if self._aplay and self._aplay.returncode is None:
            self._aplay.kill()

    async def esperar_silencio(self) -> None:
        """Espera tudo que está nas filas ser tocado."""
        await self._fila_texto.join()
        await self._fila_audio.join()

    async def encerrar(self) -> None:
        self.interromper()
        for t in self._tarefas:
            t.cancel()
        await asyncio.gather(*self._tarefas, return_exceptions=True)

    async def _sintetizar_worker(self) -> None:
        while True:
            frase = await self._fila_texto.get()
            geracao = self._geracao
            try:
                wav = await self._speaches.sintetizar(frase)
                if geracao == self._geracao:
                    await self._fila_audio.put((geracao, wav))
            except (httpx.HTTPError, OSError) as e:
                self.erro_tts = f"{type(e).__name__}: {e}"
            finally:
                self._fila_texto.task_done()

    async def _tocar_worker(self) -> None:
        while True:
            geracao, wav = await self._fila_audio.get()
            try:
                if geracao != self._geracao:
                    continue  # áudio de uma resposta já interrompida
                if self._primeiro_audio_pendente and self._ao_comecar_primeiro_audio:
                    self._primeiro_audio_pendente = False
                    self._ao_comecar_primeiro_audio()
                self._aplay = await asyncio.create_subprocess_exec(
                    "aplay", "-q", "-t", "wav", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await self._aplay.communicate(wav)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                self._aplay = None
                self._fila_audio.task_done()
