# claude-voz

Conversa por voz com o [Claude Code](https://code.claude.com), em português do Brasil, com a voz e a transcrição
rodando na sua própria máquina.

Você aperta Enter, fala, aperta Enter de novo. O áudio vira texto no Whisper local, vai para o Claude Code pelo
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk), e a resposta volta em voz, frase por frase, enquanto
o texto aparece na tela. Só o Claude é remoto: nenhum áudio ou texto passa por serviço externo de voz.

## O que ele faz

- Sessão contínua com o Claude Code no diretório do projeto, carregando `CLAUDE.md` e `settings.json` como o CLI faz.
- Entrada por voz (Enter grava, Enter envia) ou por texto digitado.
- Resposta falada em streaming: cada frase completa entra numa fila e é tocada em ordem, sem travar o recebimento.
- Código, caminhos longos, links e markdown ficam só na tela; a voz fala o resto.
- Cada ferramenta que o Claude usa aparece numa linha compacta (`🔧 Bash: git status`).
- Ler e buscar não pedem permissão. Alterar arquivo ou rodar comando que muda algo: a voz pede e você responde
  `s`, `n` ou `t` (sim, e não perguntar mais nesta sessão para esse programa).
- Ctrl+C no meio da resposta para a voz e interrompe a tarefa; Ctrl+C com a sessão parada encerra.
- Se o servidor de voz estiver fora do ar, avisa e segue só em texto.

## Como funciona

```
microfone ──arecord──▶ WAV ──▶ Speaches /v1/audio/transcriptions (Whisper, GPU) ──▶ texto
                                                                                    │
                                  Claude Code (Agent SDK, sessão multi-turno) ◀─────┘
                                                │ texto em streaming
                                                ▼
            frases ──▶ Speaches /v1/audio/speech (Kokoro, GPU) ──▶ WAV ──aplay──▶ alto-falante
```

[Speaches](https://speaches.ai) é um servidor pronto, com a mesma API de áudio da OpenAI, que roda o
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) para transcrever e o
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) para falar. Um container, sem código de servidor próprio.

### Voz alternativa: Rápido TTS

O [Rápido TTS](https://huggingface.co/edwixx/rapido-tts) é um modelo pequeno (21 M de parâmetros, Apache 2.0)
treinado só em português do Brasil, com uma voz, a 48 kHz, sem o sotaque do Kokoro. Ele não tem servidor HTTP
próprio, então a pasta `rapido-tts/` traz um: `server.py` (FastAPI, ~80 linhas) expõe a mesma API
`/v1/audio/speech`, e o `Dockerfile` instala o torch com CUDA e o código do modelo fixado por commit. Com ele o
desenho fica com dois containers: o Speaches só transcreve e o `rapido-tts` fala.

```bash
docker compose -f rapido-tts/docker-compose.yml up -d --build   # imagem de 11 GB (torch + CUDA); modelo de 42 MB
export CLAUDE_VOZ_TTS_URL=http://127.0.0.1:8001
```

Medido numa RTX 4000 Ada: 0,10 s por frase curta, 0,19 s por frase de 150 caracteres, 1,65 GB de VRAM, 27 s para
carregar. O Rápido ignora `CLAUDE_VOZ_VOZ` e `CLAUDE_VOZ_MODELO_TTS` (tem uma voz só); `CLAUDE_VOZ_VELOCIDADE` vale.

## Requisitos

- Linux com ALSA (`arecord` e `aplay`), Python 3.12 e [uv](https://docs.astral.sh/uv/).
- [Claude Code](https://code.claude.com) instalado e logado (`claude` no `PATH`). O SDK usa esse login;
  não precisa de `ANTHROPIC_API_KEY`.
- Docker com [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) e uma GPU
  NVIDIA. Medido numa RTX 4000 Ada: 3,4 GB de VRAM com os dois modelos carregados, 0,1 s para sintetizar uma frase,
  0,3 s para transcrever 5 s de áudio.

## Instalação

1. Servidor de voz:

   ```bash
   docker compose -f speaches/docker-compose.yml up -d
   curl -X POST http://127.0.0.1:8000/v1/models/speaches-ai/Kokoro-82M-v1.0-ONNX
   curl -X POST http://127.0.0.1:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2
   ```

   Os modelos ficam num volume Docker (cerca de 2 GB). Teste a voz:

   ```bash
   curl -s http://127.0.0.1:8000/v1/audio/speech -H "Content-Type: application/json" --output teste.wav \
     --data '{"input":"Olá, o servidor de voz está funcionando.","model":"speaches-ai/Kokoro-82M-v1.0-ONNX","voice":"pm_alex","response_format":"wav"}'
   aplay teste.wav
   ```

2. O programa:

   ```bash
   uv tool install --editable .
   ```

   Isso cria o comando `claude-voz` em `~/.local/bin`.

## Uso

```bash
claude-voz [diretório-do-projeto]      # padrão: diretório atual
```

| Tecla | Efeito |
|---|---|
| Enter | começa a gravar; Enter de novo para, transcreve e envia |
| texto + Enter | envia o texto digitado |
| `s` / `n` / `t` | resposta ao pedido de permissão |
| Ctrl+C | durante a resposta: para a voz e interrompe; com a sessão parada: sai |
| Ctrl+D | sai |

No fim de cada resposta aparece `⏱ resposta 8,3 s · primeiro áudio 1,7 s` (tempo total e tempo até a primeira
frase falada).

## Permissões

O claude-voz não substitui o sistema de permissões do Claude Code; ele configura esse sistema e responde ao
pedido quando ele pergunta. Em ordem:

1. **Livres**: `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, e os comandos Bash só de leitura da lista
   `REGRAS_BASH_LEITURA` (`ls`, `cat`, `grep`, `git log`, `git status`, `git diff`, `docker ps`...).
   Quem avalia é o Claude Code, que corta a cadeia em `;`, `&&` e `|` e exige que cada pedaço bata numa regra.
   As regras são por prefixo e não enxergam flag; por isso ficam de fora programas que escrevem ou executam por
   flag (`curl`, `wget`, `sort -o`, `rg --pre`, `find -delete`, `sed -i`), e as flags perigosas do git
   (`--output`, `--upload-pack`, `-c`) estão na lista de negação.
2. **Sempre perguntam** (`REGRAS_PERGUNTAR`): `curl`/`wget` que enviam dado para fora (`-d`, `--data`, `-F`, `-T`,
   `-X POST`...), mesmo que você libere `curl` por `CLAUDE_VOZ_LIBERAR`.
3. **Nunca** (`REGRAS_NEGAR`): ler `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.config/anthropic`, `~/.config/gh`,
   `~/.claude/.credentials.json`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc` e parecidos, pelo Read e
   pelos programas de leitura do Bash; `curl file://`; `git` com `--output`, `--upload-pack`, `-c`.
4. **Todo o resto pergunta**: editar e criar arquivo, `rm`, `git push`, `sudo`, `python3 -c`... A voz diz o que vai
   fazer e o terminal espera `s`, `n` ou `t`. Programas que apagam, sobrescrevem, publicam ou executam qualquer coisa
   (`rm`, `cp`, `mv`, `git push`, `sudo`, `bash`, `python3`, `xargs`...; lista `SEM_T_NA_SESSAO`) não têm `t`: só
   `s`/`n`, a cada comando. O `t` só vale para comando simples; uma cadeia com `&&` ou `|` pergunta de novo.

Leitura é livre em toda a pasta pessoal (`CLAUDE_VOZ_DIRS`), para você perguntar sobre qualquer projeto a partir de
qualquer lugar. As regras `allow`, `ask` e `deny` do seu `settings.json` continuam valendo por cima disso.

Para liberar `curl` e `wget` (baixar para arquivo sem perguntar; `file://` e envio de dado continuam tratados):

```bash
export CLAUDE_VOZ_LIBERAR="Bash(curl *),Bash(wget *)"
```

## Configuração

Tudo por variável de ambiente, com padrão:

| Variável | Padrão | O que é |
|---|---|---|
| `CLAUDE_VOZ_SPEACHES_URL` | `http://127.0.0.1:8000` | onde está o Speaches (transcrição; e voz, se a URL abaixo não for dada) |
| `CLAUDE_VOZ_TTS_URL` | igual à de cima | servidor de voz com a API `/v1/audio/speech` (ex.: `http://127.0.0.1:8001` para o Rápido) |
| `CLAUDE_VOZ_VOZ` | `pm_alex` | voz do Kokoro; em pt-BR: `pf_dora` (feminina), `pm_alex`, `pm_santa` |
| `CLAUDE_VOZ_MODELO_TTS` | `speaches-ai/Kokoro-82M-v1.0-ONNX` | modelo de voz |
| `CLAUDE_VOZ_MODELO_WHISPER` | `deepdml/faster-whisper-large-v3-turbo-ct2` | modelo de transcrição |
| `CLAUDE_VOZ_VELOCIDADE` | `1.0` | velocidade da fala |
| `CLAUDE_VOZ_PROMPT_WHISPER` | vazio | termos que ajudam a transcrição (nomes, siglas) |
| `CLAUDE_VOZ_DIRS` | `~` | pastas em que ler não pede permissão |
| `CLAUDE_VOZ_BASH_LEITURA` | `1` | `0` desliga a lista de comandos Bash livres |
| `CLAUDE_VOZ_LIBERAR` | vazio | regras extras liberadas, ex.: `Bash(go test *),Bash(npm run *)` |
| `CLAUDE_VOZ_NEGAR` | vazio | regras extras bloqueadas |
| `CLAUDE_VOZ_CLI` | `claude` do `PATH` | caminho do executável do Claude Code |

Outras vozes: o Speaches também roda o [Piper](https://github.com/rhasspy/piper); baixe com
`curl -X POST http://127.0.0.1:8000/v1/models/speaches-ai/piper-pt_BR-faber-medium` e use
`CLAUDE_VOZ_MODELO_TTS=speaches-ai/piper-pt_BR-faber-medium CLAUDE_VOZ_VOZ=faber`.

## Estrutura

```
src/claude_voz/
  __init__.py            laço principal, permissões, Ctrl+C, tela
  audio.py               gravação (arecord), Speaches (HTTP), fila de síntese e reprodução (aplay)
  fala.py                limpeza do texto para voz e corte em frases (ignora blocos de código)
  claude-sem-ctrl-c.sh   abre o `claude` fora do grupo de processos do terminal
speaches/docker-compose.yml
rapido-tts/               servidor de voz alternativo (Dockerfile, server.py, docker-compose.yml)
```

O `claude-sem-ctrl-c.sh` existe porque o Ctrl+C do terminal vai para todos os processos do grupo, inclusive o
`claude` que o SDK abre, e ele encerraria. Com `setsid`, o sinal chega só ao claude-voz, que chama `interrupt()`.

Log do processo `claude`: `~/.cache/claude-voz/claude-cli.log`.

## Remover

```bash
uv tool uninstall claude-voz
docker compose -f speaches/docker-compose.yml down -v   # -v apaga o volume com os modelos
docker rmi ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3
docker compose -f rapido-tts/docker-compose.yml down -v --rmi local   # se usou o Rápido
rm -rf ~/.cache/claude-voz
```

## Limites conhecidos

- Só Linux com ALSA. O microfone e a saída usam o dispositivo padrão do sistema.
- O Speaches não tem autenticação e o compose o expõe só em `127.0.0.1`. Se apontar `CLAUDE_VOZ_SPEACHES_URL` para
  outra máquina, o áudio e o texto trafegam em claro na rede.
- O log `~/.cache/claude-voz/claude-cli.log` cresce sem rotação.
- O tempo até a primeira frase falada depende do tamanho da primeira frase que o modelo escreve (medido: 1,4 a 7 s).
- As regras de leitura são por prefixo: `find -delete` e `sed -i` pedem, `git -C <pasta> log` é tratado à parte.
- Com `STT_MODEL_TTL` e `TTS_MODEL_TTL` em 300 s no compose, a primeira pergunta depois de 5 min ocioso paga a
  recarga dos modelos (1 a 5 s). Use `-1` para deixá-los residentes na GPU.

## Licença

MIT.
