# Servidor de voz com o Rápido TTS (pt-BR), expondo a mesma API /v1/audio/speech do Speaches/OpenAI.
# Código do modelo: https://github.com/anurag12-webster/rapido-tts (Apache 2.0), fixado por commit.
# Pesos: edwixx/rapido-tts (42 MB) + decodificador openbmb/VoxCPM2, baixados na primeira subida para o volume hf-cache.
FROM python:3.12-slim

ARG RAPIDO_COMMIT=8ee2d2a79005d78a9871ded408a35a6139c2ec6d
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# torch com CUDA 12.6 (o driver da máquina é mais novo; CUDA é compatível para trás)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu126
RUN git clone https://github.com/anurag12-webster/rapido-tts /app/rapido-tts \
 && git -C /app/rapido-tts checkout --quiet "$RAPIDO_COMMIT"
RUN pip install --no-cache-dir numpy num2words pydantic huggingface_hub "fastapi>=0.115" "uvicorn>=0.30"
COPY server.py /app/server.py
ENV PYTHONPATH=/app/rapido-tts HF_HOME=/cache/huggingface PYTHONUNBUFFERED=1
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD curl -sf http://127.0.0.1:8001/health || exit 1
CMD ["python", "/app/server.py"]
