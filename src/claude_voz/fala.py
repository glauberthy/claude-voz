"""Limpeza de texto para voz e separação em frases.

O que a tela mostra é o texto bruto do Claude. O que vai para o Speaches é o
texto depois de `limpar_para_fala`: sem bloco de código, sem caminho longo,
sem link e sem marcação markdown.
"""

from __future__ import annotations

import re

_RE_URL = re.compile(r"(https?://\S+|www\.\S+)")
# caminho com pelo menos duas barras (ex.: ~/ferramentas/claude-voz, /etc/hosts/x, ./src/a.py)
_RE_CAMINHO = re.compile(r"(?<![\w])(~|\.{1,2})?/[\w.\-@]+(/[\w.\-@]*)+")
_RE_CODIGO_INLINE = re.compile(r"`[^`\n]*`")
_RE_TITULO = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_RE_LISTA = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_RE_CITACAO = re.compile(r"^\s*>\s?", re.MULTILINE)
_RE_ENFASE = re.compile(r"(\*\*|__|~~)(.+?)\1")
_RE_ITALICO = re.compile(r"(?<!\w)[*_](?!\s)(.+?)(?<!\s)[*_](?!\w)")
_RE_LINK_MD = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RE_TABELA = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RE_SEPARADOR = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_RE_ESPACOS = re.compile(r"[ \t]+")
_RE_LETRAS = re.compile(r"[A-Za-zÀ-ÿ]")

# Pronúncia de termos em inglês: o Rápido (e o Kokoro) leem com regra de português, então "bash" vira "báchi".
# A troca é só no texto que vai para a voz; a tela mostra o termo original. Grafia aproximada em português.
# Para acrescentar ou corrigir sem mexer no código: arquivo apontado por CLAUDE_VOZ_PRONUNCIA, uma linha por
# termo, no formato `termo=como falar` (ex.: `keycloak=quiclôuc`). Comparação sem diferenciar maiúsculas.
PRONUNCIA_PADRAO: dict[str, str] = {
    "bash": "béch", "git": "guít", "github": "guít-rãb", "commit": "comít", "commits": "comíts", "push": "púch",
    "pull": "púl", "fetch": "fétch", "merge": "mârdj", "rebase": "ribêiz", "branch": "brênch", "branches": "brênches",
    "checkout": "tchécaut", "stash": "stéch", "diff": "díf", "log": "lóg", "status": "státus", "clone": "clôun",
    "docker": "dóquer", "compose": "compôuz", "container": "contêiner", "build": "bíld", "deploy": "diplói",
    "pipeline": "páiplain", "jenkins": "djênquins", "stage": "stêidj", "release": "rilís", "debug": "dibâg",
    "bug": "bâg", "bugs": "bâgs", "script": "scrípt", "scripts": "scrípts", "shell": "chél", "terminal": "terminal",
    "python": "páiton", "node": "nôud", "npm": "ene-pê-eme", "go": "gôu", "golang": "gôu-lang", "vue": "viú",
    "pinia": "pínia", "vite": "vít", "typescript": "táipscript", "javascript": "djávascript", "json": "djêison",
    "yaml": "iâmel", "toml": "tômel", "sql": "ésse-quê-éle", "api": "a-pê-í", "rest": "rést", "http": "agá-tê-tê-pê",
    "url": "u-érre-éle", "uri": "u-érre-í", "id": "i-dê", "ids": "i-dês", "jwt": "jóta-dáblio-tê", "token": "tôquen",
    "tokens": "tôquens", "login": "lóguin", "logout": "lógaut", "email": "imêil", "e-mail": "imêil", "cache": "quéche",
    "backend": "béquend", "frontend": "frontend", "framework": "frêimuork", "middleware": "mídeluér",
    "endpoint": "êndpoint", "handler": "rândler", "service": "sérvis", "repository": "ripózitori", "domain": "domêin",
    "transport": "transport", "router": "ráuter", "store": "stór", "composable": "compôuzabol", "component": "compônent",
    "readme": "rídmi", "claude": "clôd", "anthropic": "antrópic", "opus": "ópus", "sonnet": "sônet", "whisper": "uísper",
    "speaches": "spítches", "kokoro": "cocôro", "keycloak": "quiclôuc", "realm": "rélm", "client": "cláient",
    "postgres": "póstgres", "mysql": "mai-ésse-quê-éle", "redis": "rédis", "nginx": "êndjin-écs", "linux": "línux",
    "ubuntu": "ubúntu", "windows": "uíndous", "android": "ândroid", "kotlin": "cótlin", "gradle": "grêidol",
    "flutter": "flâter", "test": "tést", "tests": "tésts", "mock": "móc", "lint": "línt", "make": "mêic", "check": "tchéc",
    "timeout": "táimaut", "upload": "âplôud", "download": "dáunlôud", "file": "fáil", "path": "páf", "default": "difólt",
    "host": "rôust", "port": "pórt", "localhost": "lôucal-rôust", "root": "rút", "sudo": "súdo", "home": "rôum",
    "feature": "fítcher", "hotfix": "rótfics", "main": "mêin", "master": "máster", "develop": "divélop", "tag": "tég",
    "issue": "íchu", "issues": "íchus", "pull request": "púl ricuést", "review": "riviú", "ok": "ôquei", "okay": "ôquei",
}


def carregar_pronuncia_extra(caminho: str | None) -> dict[str, str]:
    """Lê um arquivo `termo=como falar` por linha; linhas vazias e com # são ignoradas."""
    extra: dict[str, str] = {}
    if not caminho:
        return extra
    try:
        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                termo, _, fala = linha.partition("=")
                if termo.strip():
                    extra[termo.strip().lower()] = fala.strip()
    except OSError:
        pass
    return extra


class Pronuncia:
    """Troca termos (palavra inteira, sem diferenciar maiúsculas) pela grafia falada."""

    def __init__(self, tabela: dict[str, str]):
        self._tabela = {k.lower(): v for k, v in tabela.items()}
        # termos maiores primeiro, para "pull request" ganhar de "pull"
        termos = sorted(self._tabela, key=len, reverse=True)
        self._re = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in termos) + r")(?![\w-])", re.IGNORECASE) if termos else None

    def aplicar(self, texto: str) -> str:
        if self._re:
            texto = self._re.sub(lambda m: self._tabela[m.group(1).lower()], texto)
        return _RE_SIGLA.sub(lambda m: "-".join(_LETRAS[c] for c in m.group(1)), texto)


# Sigla em maiúsculas (2 a 5 letras: FPS, HML, PRD, SQL, PK) é soletrada; o que está na tabela acima ganha antes.
_RE_SIGLA = re.compile(r"(?<![\w-])([A-Z]{2,5})(?![\w-])")
_LETRAS = {
    "A": "á", "B": "bê", "C": "cê", "D": "dê", "E": "é", "F": "éfe", "G": "gê", "H": "agá", "I": "i", "J": "jóta",
    "K": "cá", "L": "éle", "M": "eme", "N": "ene", "O": "ó", "P": "pê", "Q": "quê", "R": "érre", "S": "ésse", "T": "tê",
    "U": "u", "V": "vê", "W": "dáblio", "X": "xis", "Y": "ípsilon", "Z": "zê",
}


def limpar_para_fala(texto: str) -> str:
    """Remove o que não deve ser lido em voz alta. Devolve '' se não sobrar nada falável."""
    t = texto
    t = _RE_TABELA.sub(" ", t)
    t = _RE_SEPARADOR.sub(" ", t)
    t = _RE_LINK_MD.sub(r"\1", t)  # [texto](url) -> texto
    t = _RE_URL.sub(" ", t)
    t = _RE_CODIGO_INLINE.sub(" ", t)
    t = _RE_CAMINHO.sub(" ", t)
    t = _RE_TITULO.sub("", t)
    t = _RE_LISTA.sub("", t)
    t = _RE_CITACAO.sub("", t)
    t = _RE_ENFASE.sub(r"\2", t)
    t = _RE_ITALICO.sub(r"\1", t)
    t = t.replace("`", " ")
    t = _RE_ESPACOS.sub(" ", t)
    t = re.sub(r"\s*\n\s*", " ", t).strip()
    if len(_RE_LETRAS.findall(t)) < 2:
        return ""
    return t


_RE_FIM_FRASE = re.compile(r"(?<=[.!?…])\s+|(?<=[:;])\s+(?=[A-ZÀ-Ý])|\n+")


class SeparadorDeFrases:
    """Acumula pedaços de texto vindos do streaming e devolve frases completas.

    Mantém o estado de bloco de código (``` abre e fecha): o que está dentro
    nunca vira frase para a fila de áudio.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._em_codigo = False

    def alimentar(self, pedaco: str) -> list[str]:
        self._buffer += pedaco
        frases: list[str] = []
        while True:
            if self._em_codigo:
                fim = self._buffer.find("```")
                if fim < 0:
                    # ainda dentro do bloco: descarta o que já se sabe que é código
                    self._buffer = self._buffer[-2:]  # guarda cauda para detectar ``` partido
                    return frases
                self._buffer = self._buffer[fim + 3 :]
                self._em_codigo = False
                continue
            inicio = self._buffer.find("```")
            if inicio >= 0:
                antes = self._buffer[:inicio]
                frases.extend(self._cortar(antes, final=True))
                self._buffer = self._buffer[inicio + 3 :]
                self._em_codigo = True
                continue
            frases.extend(self._cortar(self._buffer, final=False))
            return frases

    def finalizar(self) -> list[str]:
        """Devolve o que sobrou no buffer como frase (fim da resposta)."""
        if self._em_codigo:
            self._buffer = ""
            self._em_codigo = False
            return []
        frases = self._cortar(self._buffer, final=True)
        self._buffer = ""
        return frases

    def limpar(self) -> None:
        self._buffer = ""
        self._em_codigo = False

    def _cortar(self, texto: str, *, final: bool) -> list[str]:
        partes = _RE_FIM_FRASE.split(texto)
        if not final:
            # a última parte pode ser uma frase ainda incompleta: fica no buffer
            self._buffer = partes[-1]
            partes = partes[:-1]
        frases = []
        for p in partes:
            p = p.strip()
            if p:
                frases.append(p)
        return frases
