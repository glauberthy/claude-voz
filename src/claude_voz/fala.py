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
