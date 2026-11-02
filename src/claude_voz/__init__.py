"""claude-voz: conversa por voz com o Claude Code, via Claude Agent SDK + Speaches local.

Uso: claude-voz [diretorio-do-projeto]

Variáveis de ambiente (todas opcionais):
  CLAUDE_VOZ_SPEACHES_URL   padrão http://127.0.0.1:8000
  CLAUDE_VOZ_VOZ            padrão pm_alex   (vozes pt-BR do Kokoro: pf_dora, pm_alex, pm_santa)
  CLAUDE_VOZ_MODELO_TTS     padrão speaches-ai/Kokoro-82M-v1.0-ONNX
  CLAUDE_VOZ_MODELO_WHISPER padrão deepdml/faster-whisper-large-v3-turbo-ct2
  CLAUDE_VOZ_VELOCIDADE     padrão 1.0
  CLAUDE_VOZ_PROMPT_WHISPER termos para ajudar a transcrição (ex.: "Claude Code, Speaches, go-kit"); padrão vazio
  CLAUDE_VOZ_BASH_LEITURA   1 (padrão) passa ao Claude Code as regras de leitura REGRAS_BASH_LEITURA
                            (ls, grep, git log...); 0 desliga e todo Bash pede
  CLAUDE_VOZ_DIRS           pastas extras em que ler não pede permissão, separadas por vírgula; padrão: a pasta pessoal
  CLAUDE_VOZ_NEGAR          regras bloqueadas sempre, além de REGRAS_NEGAR (~/.ssh, ~/.aws, credenciais...)
  CLAUDE_VOZ_CLI            caminho do executável `claude` (padrão: o `claude` do PATH)
  CLAUDE_VOZ_LIBERAR        regras liberadas sem perguntar, só no claude-voz, separadas por vírgula
                            (sintaxe do Claude Code, ex.: "Bash(curl *),Bash(go test *)"); padrão vazio
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sys
import tempfile
import time
import warnings
from pathlib import Path

from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .audio import Gravador, Reprodutor, Speaches

# Read/Glob/Grep em allowed_tools nunca chegam ao callback: é o comportamento desejado, não um aviso.
warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
# O SDK (anyio) e o asyncio disputam o waitpid dos subprocessos; o aviso "exit status already read" é inofensivo.
logging.getLogger("asyncio").setLevel(logging.ERROR)
from .fala import SeparadorDeFrases, limpar_para_fala

INSTRUCAO_VOZ = (
    "Esta sessão é por voz. Entre uma ferramenta e outra, escreva no máximo uma frase curta "
    "dizendo o que está fazendo. Termine com 2 a 4 frases em português do Brasil, em linguagem "
    "falada e direta, sem markdown e sem analogias, dizendo o que foi feito e se preciso decidir "
    "algo. Coloque código e detalhes técnicos longos em blocos de código, que não serão lidos em voz alta. "
    "Para ler arquivos, listar pastas e procurar texto use as ferramentas Read, Glob e Grep, que não pedem "
    "permissão; use Bash só quando elas não resolvem. No Bash, um comando simples por chamada, com caminho "
    "absoluto em vez de `cd ...;`, e sem laços `for`. Para git em outra pasta, `git -C <pasta> <subcomando>`."
)

# Ferramentas de leitura e busca: liberadas sem perguntar.
# WebFetch/WebSearch só leem: a URL e o conteúdo já iriam para o Claude de qualquer forma.
FERRAMENTAS_LIVRES = ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]

# Nunca ler, mesmo com a home liberada: chaves e credenciais. Sintaxe gitignore do Claude Code (`~/` = home).
# Cobre as ferramentas Read/Edit/Write. Pelo Bash cobre só as formas comuns listadas (cat/head/tail com ~ ou caminho
# absoluto da home); um `python3 -c open(...)` ainda cairia no pedido de permissão normal, não no deny.
_PASTAS_SENSIVEIS = (".ssh/", ".gnupg/", ".aws/", ".kube/", ".config/anthropic/", ".config/gh/")
_ARQUIVOS_SENSIVEIS = (".claude/.credentials.json", ".claude.json", ".netrc", ".git-credentials", ".gitconfig",
                       ".docker/config.json", ".npmrc", ".pypirc")
# programas de leitura que recebem caminho de arquivo (os da lista livre + os que o Claude Code libera sozinho)
_PROGRAMAS_QUE_LEEM = ("cat", "head", "tail", "less", "more", "strings", "grep", "egrep", "fgrep", "rg", "wc", "stat",
                       "file", "diff", "cmp", "cut", "nl", "uniq", "sort", "md5sum", "sha256sum", "column", "jq", "od",
                       "xxd", "hexdump", "base64", "tee", "cp", "mv", "ln", "sed", "awk", "python3", "python")
REGRAS_NEGAR = (
    [f"Read(~/{p}**)" for p in _PASTAS_SENSIVEIS] + [f"Read(~/{a})" for a in _ARQUIVOS_SENSIVEIS]
    + [f"{t}(~/{p}**)" for t in ("Edit", "Write") for p in _PASTAS_SENSIVEIS]
    + [
        # `Bash(cat ~/.ssh/*)` casa `cat ~/.ssh/config`; `Bash(head * ~/.ssh/*)` casa `head -1 ~/.ssh/config`
        f"Bash({prog}{meio} {pasta}{alvo}{fim})"
        for prog in _PROGRAMAS_QUE_LEEM
        for meio in ("", " *")
        for pasta in ("~/", str(Path.home()) + "/")
        for alvo, fim in [(p, "*") for p in _PASTAS_SENSIVEIS] + [(a, "") for a in _ARQUIVOS_SENSIVEIS]
    ]
    + [
        # curl/wget lendo arquivo local por file://, e git com flag que executa programa ou grava arquivo
        "Bash(curl file://*)", "Bash(curl * file://*)", "Bash(wget file://*)", "Bash(wget * file://*)",
        "Bash(git -c *)", "Bash(git * --output=*)", "Bash(git * --output *)", "Bash(git * --upload-pack*)",
        "Bash(git * --receive-pack*)", "Bash(git * --exec*)", "Bash(git * --git-dir*)", "Bash(git * --work-tree*)",
    ]
)

# Sempre perguntar, mesmo que `Bash(curl *)` seja liberado por CLAUDE_VOZ_LIBERAR: curl/wget que enviam dado para fora.
# No Claude Code a regra `ask` vence a `allow`. Vai pelo `settings` do SDK (JSON), que é onde `ask` existe.
REGRAS_PERGUNTAR = [
    "Bash(curl * -d *)", "Bash(curl * --data *)", "Bash(curl * --data-binary *)", "Bash(curl * --data-raw *)",
    "Bash(curl * --data-urlencode *)", "Bash(curl * --json *)", "Bash(curl * -F *)", "Bash(curl * --form *)",
    "Bash(curl * -T *)", "Bash(curl * --upload-file *)", "Bash(curl * -X POST *)", "Bash(curl * -X PUT *)",
    "Bash(curl * -X DELETE *)", "Bash(curl * -X PATCH *)", "Bash(curl * -XPOST *)", "Bash(curl * -XPUT *)",
    "Bash(curl * -XDELETE *)", "Bash(curl * -XPATCH *)", "Bash(curl * --request *)", "Bash(curl * --data-ascii *)",
    "Bash(curl * -K *)", "Bash(curl * --config *)",
    "Bash(wget * --post-file *)", "Bash(wget * --post-data *)", "Bash(wget * --body-file *)", "Bash(wget * --body-data *)",
    "Bash(wget * --method *)",
]

# Comandos Bash só de leitura, como regras de permissão do Claude Code (`Bash(git log *)`: prefixo). Quem avalia é o
# Claude Code: ele corta a cadeia em `;`, `&&` e `|`, exige que cada pedaço bata numa regra e barra `$(...)`.
# Só entram programas sem variante que apague ou altere: `find` (-delete/-exec), `sed` (-i) e `git branch` (-D)
# ficam de fora e continuam pedindo. Regra por prefixo não enxerga flag, então a lista é curta de propósito.
# O Claude Code ainda pede quando o comando toca caminho fora do diretório da sessão.
REGRAS_BASH_LEITURA = [
    "Bash(ls *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)", "Bash(stat *)", "Bash(file *)",
    "Bash(grep *)", "Bash(du *)", "Bash(df *)", "Bash(which *)", "Bash(diff *)",
    "Bash(uniq *)", "Bash(cut *)", "Bash(nl *)", "Bash(realpath *)", "Bash(date *)",
    "Bash(git log *)", "Bash(git status *)", "Bash(git diff *)", "Bash(git show *)", "Bash(git blame *)",
    "Bash(git rev-list *)", "Bash(git rev-parse *)", "Bash(git ls-files *)", "Bash(git shortlog *)",
    "Bash(git branch --show-current)", "Bash(git branch --list *)", "Bash(git branch -a *)", "Bash(git branch -r *)",
    "Bash(git branch -v *)", "Bash(git branch -vv *)",
    "Bash(git worktree list *)", "Bash(git remote -v)", "Bash(git stash list *)", "Bash(git fetch *)",
    "Bash(git describe *)", "Bash(git tag -l *)",
    "Bash(docker ps *)", "Bash(docker images *)", "Bash(nvidia-smi *)", "Bash(go version *)", "Bash(uv tree *)",
]
# Fora da lista de propósito, porque uma flag muda o que fazem: `curl`/`wget` (baixam para arquivo, `file://` lê
# qualquer caminho), `sort -o`, `tree -o`, `rg --pre`, `go env -w`, `find -delete`, `sed -i`. Quem quiser `curl`
# livre põe `CLAUDE_VOZ_LIBERAR="Bash(curl *)"`; as formas de envio continuam perguntando (REGRAS_PERGUNTAR) e
# `file://` continua negado (REGRAS_NEGAR).

CINZA = "\033[2m"
AMARELO = "\033[33m"
VERDE = "\033[32m"
VERMELHO = "\033[31m"
RESET = "\033[0m"


def _env(nome: str, padrao: str) -> str:
    return os.environ.get(nome, padrao)


class Tela:
    """Escreve na tela controlando se a última coisa impressa terminou em quebra de linha."""

    def __init__(self) -> None:
        self._no_inicio_da_linha = True

    def texto(self, s: str) -> None:
        if not s:
            return
        sys.stdout.write(s)
        sys.stdout.flush()
        self._no_inicio_da_linha = s.endswith("\n")

    def linha(self, s: str = "") -> None:
        if not self._no_inicio_da_linha:
            sys.stdout.write("\n")
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
        self._no_inicio_da_linha = True

    def aviso(self, s: str) -> None:
        self.linha(f"{AMARELO}{s}{RESET}")

    def erro(self, s: str) -> None:
        self.linha(f"{VERMELHO}{s}{RESET}")

    def discreto(self, s: str) -> None:
        self.linha(f"{CINZA}{s}{RESET}")


def alvo_da_ferramenta(nome: str, entrada: dict) -> str:
    """O 'alvo principal' de cada ferramenta, para a linha compacta na tela."""
    chaves = {
        "Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
        "WebFetch": "url", "WebSearch": "query", "Agent": "description", "Task": "description",
        "Skill": "skill", "TodoWrite": "", "KillShell": "shell_id", "BashOutput": "bash_id",
    }
    chave = chaves.get(nome)
    valor = entrada.get(chave) if chave else None
    if valor is None:
        for v in entrada.values():
            if isinstance(v, str) and v:
                valor = v
                break
    valor = str(valor or "").replace("\n", " ⏎ ")
    return valor if len(valor) <= 100 else valor[:97] + "..."


_METACARACTERES = set(";&|<>$`(){}\n")


# flags do git que executam programa ou gravam arquivo, mesmo em subcomando de leitura (log --output, fetch --upload-pack)
_GIT_FLAGS_PERIGOSAS = ("--output", "--upload-pack", "--receive-pack", "--exec", "-c", "--git-dir", "--work-tree",
                        "--config-env", "-o")


def _git_sem_C(comando: str) -> tuple[str, str] | None:
    """Para `git -C <pasta> <resto>` simples e sem flag perigosa, devolve (pasta, "git <resto>"). Senão, None."""
    if any(c in _METACARACTERES for c in comando):
        return None
    try:
        palavras = shlex.split(comando)
    except ValueError:
        return None
    if len(palavras) < 4 or palavras[0] != "git" or palavras[1] != "-C":
        return None
    if any(a == f or a.startswith(f + "=") for a in palavras[3:] for f in _GIT_FLAGS_PERIGOSAS):
        return None
    return palavras[2], shlex.join(["git", *palavras[3:]])


def _bate_em_regra(comando: str, regras: list[str]) -> bool:
    """Mesma leitura de regra do Claude Code: `Bash(x *)` (ou a forma antiga `Bash(x:*)`) é prefixo, `Bash(x)` é igualdade."""
    for regra in regras:
        if not regra.startswith("Bash(") or not regra.endswith(")"):
            continue
        conteudo = regra[5:-1]
        if conteudo.endswith(" *") or conteudo.endswith(":*"):
            prefixo = conteudo[:-2]
            if comando == prefixo or comando.startswith(prefixo + " "):
                return True
        elif comando == conteudo:
            return True
    return False


def git_C_so_leitura(comando: str, pastas_permitidas: list[Path]) -> bool:
    """`git -C <pasta> <sub>` conta como `git <sub>` para as regras de leitura, se a pasta está nas permitidas.

    O Claude Code não casa `git -C` com `Bash(git log:*)`; isto só desconta o `-C` e reaplica as mesmas regras.
    """
    partes = _git_sem_C(comando)
    if not partes:
        return False
    pasta, equivalente = partes
    try:
        alvo = Path(pasta).expanduser().resolve()
    except OSError:
        return False
    if not any(alvo == p.resolve() or p.resolve() in alvo.parents for p in pastas_permitidas):
        return False
    return _bate_em_regra(equivalente, REGRAS_BASH_LEITURA)


# Programas que apagam, sobrescrevem ou publicam: nunca ganham o `t` (liberar para a sessão). Só `s` ou `n`, por comando.
SEM_T_NA_SESSAO = {
    "rm", "rmdir", "shred", "dd", "mkfs", "truncate", "chmod", "chown", "kill", "pkill", "killall", "sudo", "su",
    "mv", "cp", "tee", "find", "sed", "awk", "ln", "git", "docker",
    "git push", "git reset", "git clean", "git rebase", "git checkout", "git restore", "git branch", "git stash",
    "git merge", "git cherry-pick", "git filter-branch", "docker rm", "docker rmi", "docker system", "docker volume",
    "docker compose", "docker run", "docker exec", "ssh", "scp", "rsync",
    # interpretadores e invólucros: a regra viraria "qualquer coisa"
    "bash", "sh", "zsh", "dash", "python", "python3", "node", "perl", "ruby", "php", "xargs", "eval", "exec",
    "timeout", "nice", "nohup", "env", "time", "watch", "setsid", "flock", "ionice",
}


def regra_de_sessao(nome: str, entrada: dict) -> str | None:
    """Regra "não perguntar mais nesta sessão", na sintaxe do Claude Code.

    Bash: libera o programa do comando (`Bash(curl *)`); para git, o subcomando (`Bash(git push *)`).
    Programas de SEM_T_NA_SESSAO não ganham regra (devolve None: só s/n).
    Edit/Write: libera a ferramenta inteira. Outras: a ferramenta pelo nome.
    """
    if nome != "Bash":
        return nome
    comando = str(entrada.get("command", "")).strip()
    # cadeia (`a && b`): a regra é do primeiro comando; se outro pedaço for destrutivo, sem `t`
    pedacos = [p.strip() for p in re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", comando) if p.strip()]
    if not pedacos:
        return None
    programas = [_programa_de(p) for p in pedacos]
    if any(not prog or prog in SEM_T_NA_SESSAO for prog in programas):  # "git" (sem subcomando) está no conjunto; "git add" não
        return None
    programa = programas[0]
    return f"Bash({programa} *)" if all(c.isalnum() or c in "._-/ " for c in programa) else None


def _programa_de(comando_simples: str) -> str | None:
    """Nome do programa (com subcomando para git/docker) de um comando sem operadores."""
    partes = comando_simples.split()
    if not partes:
        return None
    programa = partes[0].rsplit("/", 1)[-1]
    if programa in ("git", "docker"):
        resto = partes[1:]
        if programa == "git":  # desconta `-C <pasta>` e `-c chave=valor`
            resto = [a for i, a in enumerate(resto) if a not in ("-C", "-c") and (i == 0 or resto[i - 1] not in ("-C", "-c"))]
        sub = next((p for p in resto if not p.startswith("-")), None)
        return f"{programa} {sub}" if sub else programa
    return programa


class LeitorTerminal:
    """Lê linhas do terminal sem bloquear o event loop (usa add_reader no stdin)."""

    def __init__(self) -> None:
        self._fila: asyncio.Queue[str | None] = asyncio.Queue()
        asyncio.get_running_loop().add_reader(sys.stdin.fileno(), self._pronto)

    def _pronto(self) -> None:
        linha = sys.stdin.readline()
        self._fila.put_nowait(None if linha == "" else linha.rstrip("\n"))

    async def linha(self) -> str | None:
        """Devolve a linha digitada, ou None no fim da entrada (Ctrl+D)."""
        return await self._fila.get()

    def descartar_pendentes(self) -> None:
        while not self._fila.empty():
            self._fila.get_nowait()

    def fechar(self) -> None:
        asyncio.get_running_loop().remove_reader(sys.stdin.fileno())


class Sessao:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.tela = Tela()
        self.speaches = Speaches(
            url=_env("CLAUDE_VOZ_SPEACHES_URL", "http://127.0.0.1:8000"),
            voz=_env("CLAUDE_VOZ_VOZ", "pm_alex"),
            modelo_tts=_env("CLAUDE_VOZ_MODELO_TTS", "speaches-ai/Kokoro-82M-v1.0-ONNX"),
            modelo_stt=_env("CLAUDE_VOZ_MODELO_WHISPER", "deepdml/faster-whisper-large-v3-turbo-ct2"),
            velocidade=float(_env("CLAUDE_VOZ_VELOCIDADE", "1.0")),
            prompt_stt=_env("CLAUDE_VOZ_PROMPT_WHISPER", ""),
        )
        self.voz_ativa = False
        self.reprodutor = Reprodutor(self.speaches, ao_comecar_primeiro_audio=self._marcar_primeiro_audio)
        self.separador = SeparadorDeFrases()
        self.leitor: LeitorTerminal | None = None
        self.cliente: ClaudeSDKClient | None = None
        self.estado = "ocioso"  # ocioso | gravando | respondendo | permissao
        self._init_mostrado = False
        self._tarefa_principal: asyncio.Task | None = None
        self.bash_leitura_livre = _env("CLAUDE_VOZ_BASH_LEITURA", "1") != "0"
        # Pastas além do cwd em que ler não pede permissão (Bash que toca caminho fora delas pede sempre).
        self.dirs_extra = [Path(d).expanduser() for d in _env("CLAUDE_VOZ_DIRS", str(Path.home())).split(",") if d.strip()]
        self._fila_permissao = asyncio.Lock()  # pedidos em paralelo (duas ferramentas de uma vez) saem um por vez
        self._regras_sessao: list[str] = []  # regras do `t`: também cobrem pedidos que já estavam na fila
        self.interrompido = asyncio.Event()
        self.t_envio = 0.0
        self.t_primeiro_audio: float | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="claude-voz-"))
        # stderr do processo `claude` vai para um arquivo de log: é onde aparece o motivo de ele encerrar.
        self.log_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-voz"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_cli = (self.log_dir / "claude-cli.log").open("a", buffering=1)
        self.log_cli.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} cwd={cwd} ===\n")

    # ---------- voz ----------
    def _marcar_primeiro_audio(self) -> None:
        if self.t_primeiro_audio is None:
            self.t_primeiro_audio = time.perf_counter() - self.t_envio

    def falar(self, texto: str) -> None:
        if not self.voz_ativa or self.interrompido.is_set():
            return
        frase = limpar_para_fala(texto)
        if frase:
            self.reprodutor.falar(frase)

    def _falar_pedacos(self, pedaco: str) -> None:
        for frase in self.separador.alimentar(pedaco):
            self.falar(frase)

    def _falar_resto(self) -> None:
        for frase in self.separador.finalizar():
            self.falar(frase)

    # ---------- permissão ----------
    async def pode_usar_ferramenta(self, nome: str, entrada: dict, ctx: ToolPermissionContext):
        """Chamado pelo SDK só quando a ferramenta não foi liberada por allowed_tools nem pelas regras do settings.json."""
        if self.interrompido.is_set():
            return PermissionResultDeny(message="Interrompido pelo usuário", interrupt=True)
        if nome == "Bash" and self.bash_leitura_livre and git_C_so_leitura(str(entrada.get("command", "")), self.dirs_extra + [self.cwd]):
            self.tela.discreto("   (git só de leitura em outra pasta: liberado)")
            return PermissionResultAllow(updated_input=entrada)
        async with self._fila_permissao:
            return await self._perguntar_permissao(nome, entrada, ctx)

    def _coberto_por_regra_da_sessao(self, nome: str, entrada: dict) -> str | None:
        if nome == "Bash":
            comando = str(entrada.get("command", ""))
            if any(c in _METACARACTERES for c in comando):
                return None  # cadeia (`a && b`), pipe ou $( ): o prefixo não diz nada sobre o resto; pergunta
            return next((r for r in self._regras_sessao if _bate_em_regra(comando, [r])), None)
        return nome if nome in self._regras_sessao else None

    async def _perguntar_permissao(self, nome: str, entrada: dict, ctx: ToolPermissionContext):
        if self.interrompido.is_set():
            return PermissionResultDeny(message="Interrompido pelo usuário", interrupt=True)
        regra_ja_dada = self._coberto_por_regra_da_sessao(nome, entrada)
        if regra_ja_dada:  # o `t` de um pedido anterior já cobre este, que estava esperando na fila
            self.tela.discreto(f"   ({regra_ja_dada} liberado por você nesta sessão)")
            return PermissionResultAllow(updated_input=entrada)
        self.estado = "permissao"
        self._falar_resto()
        # O SDK manda `description` em linguagem humana (ex.: "Cria o arquivo marca.txt vazio"); `title` vem vazio.
        descricao = ctx.description or ctx.title or alvo_da_ferramenta(nome, entrada)
        self.tela.linha(f"{AMARELO}🔐 {nome}: {descricao}{RESET}")
        if nome == "Bash" and entrada.get("command"):
            self.tela.linha(f"   $ {entrada['command']}")
        elif nome in ("Edit", "Write", "MultiEdit") and entrada.get("file_path"):
            self.tela.linha(f"   {entrada['file_path']}")
        regra = regra_de_sessao(nome, entrada)
        if regra:
            programa = regra[5:-3] if regra.startswith("Bash(") else regra  # "Bash(curl *)" -> "curl"
            self.falar(f"Preciso de permissão para {nome}: {descricao}. Responda s, n, ou t para liberar {programa} nesta sessão.")
        else:
            self.falar(f"Preciso de permissão para {nome}: {descricao}. Responda s ou n.")
        if regra:
            self.tela.texto(f"{AMARELO}Permitir? [s/n/t = sim e não perguntar mais nesta sessão para {regra}] {RESET}")
        else:
            self.tela.texto(f"{AMARELO}Permitir? [s/n] {RESET}")
        assert self.leitor
        self.leitor.descartar_pendentes()
        esperar_linha = asyncio.ensure_future(self.leitor.linha())
        esperar_ctrl_c = asyncio.ensure_future(self.interrompido.wait())
        feito, _ = await asyncio.wait({esperar_linha, esperar_ctrl_c}, return_when=asyncio.FIRST_COMPLETED)
        self.estado = "respondendo"
        if esperar_linha in feito:
            esperar_ctrl_c.cancel()
            resposta = (esperar_linha.result() or "").strip().lower()
            if resposta in ("s", "sim", "y"):
                return PermissionResultAllow(updated_input=entrada)
            if resposta in ("t", "sempre") and regra:
                self.tela.discreto(f"   liberado nesta sessão: {regra}")
                self._regras_sessao.append(regra)
                ferramenta, _, conteudo = regra.partition("(")
                atualizacao = PermissionUpdate(
                    type="addRules", behavior="allow", destination="session",
                    rules=[PermissionRuleValue(tool_name=ferramenta, rule_content=conteudo[:-1] or None)],
                )
                return PermissionResultAllow(updated_input=entrada, updated_permissions=[atualizacao])
            self.tela.discreto("   negado")
            return PermissionResultDeny(message="O usuário negou a permissão.")
        esperar_linha.cancel()
        return PermissionResultDeny(message="Interrompido pelo usuário", interrupt=True)

    # ---------- Ctrl+C ----------
    def _ctrl_c(self) -> None:
        if self.estado == "ocioso":
            self.tela.linha("")
            if self._tarefa_principal:
                self._tarefa_principal.cancel()  # cai no CancelledError do laço principal: encerra limpo
            return
        if self.estado == "gravando":
            self.interrompido.set()
            return
        # respondendo ou esperando permissão
        if self.interrompido.is_set():
            return  # segundo Ctrl+C seguido: a interrupção já está em curso
        self.tela.aviso("⏹ interrompido")
        self.interrompido.set()
        self.separador.limpar()
        self.reprodutor.interromper()
        if self.cliente:
            asyncio.create_task(self._interromper_sdk())

    async def _interromper_sdk(self) -> None:
        try:
            await self.cliente.interrupt()
        except ClaudeSDKError as e:
            self.tela.erro(f"não consegui interromper o claude: {e}")

    # ---------- entrada ----------
    async def gravar(self) -> str | None:
        """Enter já foi dado: grava até o próximo Enter e transcreve."""
        self.estado = "gravando"
        self.interrompido.clear()
        destino = self._tmp / "entrada.wav"
        gravador = Gravador(destino)
        await gravador.iniciar()
        self.tela.texto(f"{VERMELHO}● gravando... (Enter para parar, Ctrl+C cancela){RESET} ")
        assert self.leitor
        esperar_linha = asyncio.ensure_future(self.leitor.linha())
        esperar_ctrl_c = asyncio.ensure_future(self.interrompido.wait())
        feito, _ = await asyncio.wait({esperar_linha, esperar_ctrl_c}, return_when=asyncio.FIRST_COMPLETED)
        for t in (esperar_linha, esperar_ctrl_c):
            t.cancel()
        self.estado = "ocioso"
        if esperar_ctrl_c in feito:
            await gravador.cancelar()
            self.tela.linha("gravação cancelada")
            return None
        await gravador.parar()
        self.tela.texto("transcrevendo... ")
        try:
            texto, segundos = await self.speaches.transcrever(destino)
        except Exception as e:  # noqa: BLE001 - qualquer falha do Speaches vira modo texto nesta rodada
            self.tela.erro(f"falha na transcrição ({type(e).__name__}: {e}); digite o texto.")
            return None
        self.tela.linha(f"{CINZA}⏱ transcrição {segundos:.2f}s{RESET}")
        if not texto:
            self.tela.aviso("não entendi nada; tente de novo.")
            return None
        self.tela.linha(f"{VERDE}Você:{RESET} {texto}")
        return texto

    # ---------- resposta ----------
    async def responder(self, pergunta: str) -> None:
        assert self.cliente
        self.estado = "respondendo"
        self.interrompido.clear()
        self.separador.limpar()
        self.t_primeiro_audio = None
        self.reprodutor.nova_resposta()
        self.t_envio = time.perf_counter()
        await self.cliente.query(pergunta)
        try:
            async for msg in self.cliente.receive_response():
                self._tratar(msg)
        finally:
            self._falar_resto()
            if self.voz_ativa:
                # espera o áudio terminar: o microfone não pode gravar enquanto o alto-falante fala
                await self.reprodutor.esperar_silencio()
            duracao = time.perf_counter() - self.t_envio
            partes = [f"⏱ resposta {duracao:.1f}s"]
            if self.t_primeiro_audio is not None:
                partes.append(f"primeiro áudio {self.t_primeiro_audio:.2f}s")
            if self.reprodutor.erro_tts:
                partes.append(f"erro no TTS: {self.reprodutor.erro_tts}")
                self.reprodutor.erro_tts = None
            self.tela.discreto(" · ".join(partes))
            self.estado = "ocioso"

    def _tratar(self, msg) -> None:
        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id:  # texto de subagente: não vai para a tela nem para a voz
                return
            ev = msg.event
            tipo = ev.get("type")
            if tipo == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                pedaco = ev["delta"].get("text", "")
                self.tela.texto(pedaco)
                self._falar_pedacos(pedaco)
            elif tipo == "message_stop":
                self._falar_resto()
            return
        if isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id:
                return
            for bloco in msg.content:
                if isinstance(bloco, ToolUseBlock):
                    self.tela.linha(f"{CINZA}🔧 {bloco.name}: {alvo_da_ferramenta(bloco.name, bloco.input)}{RESET}")
            if msg.error:
                self.tela.erro(f"erro do modelo: {msg.error}")
            return
        if isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for bloco in msg.content:
                    if isinstance(bloco, ToolResultBlock) and bloco.is_error:
                        conteudo = bloco.content if isinstance(bloco.content, str) else ""
                        self.tela.discreto(f"   ✗ {conteudo[:120]}")
            return
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init" and not self._init_mostrado:
                self._init_mostrado = True
                d = msg.data
                self.tela.discreto(f"sessão {d.get('session_id', '')[:8]} · modelo {d.get('model', '?')} · cwd {d.get('cwd', '')}")
            return
        if isinstance(msg, ResultMessage):
            if msg.is_error and not self.interrompido.is_set():  # rodada interrompida por Ctrl+C não é erro
                self.tela.erro(f"resultado com erro: {msg.result or msg.subtype}")
            return
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if getattr(info, "status", "allowed") != "allowed":
                self.tela.aviso(f"limite de uso: {info.status}")

    # ---------- laço principal ----------
    async def executar(self) -> int:
        loop = asyncio.get_running_loop()
        self._tarefa_principal = asyncio.current_task()
        self.leitor = LeitorTerminal()
        loop.add_signal_handler(signal.SIGINT, self._ctrl_c)

        self.voz_ativa = await self.speaches.saudavel()
        if self.voz_ativa:
            self.reprodutor.iniciar()
            self.tela.discreto(f"Speaches em {self.speaches.url} · voz {self.speaches.voz} · whisper {self.speaches.modelo_stt}")
        else:
            self.tela.aviso(f"Speaches fora do ar em {self.speaches.url}: funcionando só em texto (sem microfone e sem voz).")

        wrapper = Path(__file__).with_name("claude-sem-ctrl-c.sh")
        opcoes = ClaudeAgentOptions(
            cwd=str(self.cwd),
            cli_path=wrapper,  # abre o `claude` fora do grupo de processos do terminal (ver o script)
            add_dirs=[str(d) for d in self.dirs_extra],
            disallowed_tools=REGRAS_NEGAR + [r.strip() for r in _env("CLAUDE_VOZ_NEGAR", "").split(",") if r.strip()],
            settings=json.dumps({"permissions": {"ask": REGRAS_PERGUNTAR}}),
            system_prompt={"type": "preset", "preset": "claude_code", "append": INSTRUCAO_VOZ},
            setting_sources=None,  # None = carrega user, project e local, e os CLAUDE.md (igual ao CLI)
            permission_mode="default",
            allowed_tools=FERRAMENTAS_LIVRES
            + (REGRAS_BASH_LEITURA if self.bash_leitura_livre else [])
            + [r.strip() for r in _env("CLAUDE_VOZ_LIBERAR", "").split(",") if r.strip()],
            can_use_tool=self.pode_usar_ferramenta,
            include_partial_messages=True,
            stderr=lambda linha: self.log_cli.write(linha + "\n"),
        )
        self.tela.discreto(f"projeto: {self.cwd}")
        self.tela.linha("Enter grava · digite para enviar texto · Ctrl+C interrompe (ou sai, se ocioso) · Ctrl+D sai")

        try:
            async with ClaudeSDKClient(options=opcoes) as cliente:
                self.cliente = cliente
                while True:
                    self.estado = "ocioso"
                    self.tela.texto(f"{VERDE}> {RESET}")
                    linha = await self.leitor.linha()
                    if linha is None:
                        self.tela.linha("")
                        break
                    if linha.strip() == "":
                        if not self.voz_ativa:
                            self.tela.aviso("sem voz: digite o texto.")
                            continue
                        texto = await self.gravar()
                        if not texto:
                            continue
                    else:
                        texto = linha.strip()
                    try:
                        await self.responder(texto)
                    except ClaudeSDKError as e:
                        self.tela.erro(f"erro do SDK: {e}")
                        self.tela.erro(f"veja o log do claude em {self.log_cli.name}")
                        break
        except asyncio.CancelledError:
            pass  # Ctrl+C com a sessão ociosa
        finally:
            self.estado = "ocioso"
            self.cliente = None
            await self.reprodutor.encerrar()
            await self.speaches.fechar()
            self.leitor.fechar()
            self.log_cli.close()
            for f in self._tmp.glob("*"):
                f.unlink(missing_ok=True)
            self._tmp.rmdir()
        self.tela.linha("até logo.")
        return 0


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cwd = Path(args[0]).expanduser().resolve() if args else Path.cwd()
    if not cwd.is_dir():
        print(f"diretório não existe: {cwd}", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("AVISO: ANTHROPIC_API_KEY está definida; o SDK vai usar a chave em vez do login do Claude Code.", file=sys.stderr)
    try:
        sys.exit(asyncio.run(Sessao(cwd).executar()))
    except CLINotFoundError as e:
        print(f"claude CLI não encontrado: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
