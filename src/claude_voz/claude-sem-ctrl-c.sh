#!/bin/sh
# Abre o `claude` numa sessão própria (setsid): o Ctrl+C do terminal chega só ao claude-voz,
# que então chama interrupt() do SDK. Sem isso, o SIGINT do terminal encerra o `claude` (exit 0).
# Sem fork: este processo não é líder de grupo, então o setsid troca a sessão e faz exec no mesmo PID.
exec setsid "${CLAUDE_VOZ_CLI:-claude}" "$@"
