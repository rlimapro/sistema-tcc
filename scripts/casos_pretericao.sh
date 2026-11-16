#!/usr/bin/env bash
# Conferência dos casos-base da preterição antes (e depois) da carga completa (D-36).
#
# Uso:
#   scripts/casos_pretericao.sh --env-file <arquivo> [--cache-extra data/raw/tce_sim_v2]
#
# Calcula a preterição de cada caso na janela em que ele foi julgado (Maranguape 100 × 2025+3, Mauriti
# 104 × 2025+5, Iguatu 074 × 2018+1; controles fora do conjunto: Milhã 107 × 2018+0, Pacatuba 123 × 2020+0)
# a partir do cache (TCE_CACHE_PATH, que tem precedência, e o cache extra), grava no banco indicado e
# imprime a posição de cada credor (scripts/conferir_pretericao.py). Não usa a rede: o cache é enchido
# antes por `sarc etl-tce --bloco execucao`. Use um banco SEPARADO e vazio (ex.: o neo4j-teste em 7700).
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=""
CACHE_EXTRA="$RAIZ/data/raw/tce_sim_v2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$(realpath "$2")"; shift 2 ;;
    --cache-extra) CACHE_EXTRA="$(realpath "$2")"; shift 2 ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$ENV_FILE" ]]; then
  echo "informe --env-file (um banco separado e vazio, não o principal)" >&2
  exit 2
fi

SARC="$RAIZ/.venv/bin/sarc"
PY="$RAIZ/.venv/bin/python"
[[ -x "$SARC" ]] || (cd "$RAIZ" && uv sync --quiet)

etapa() {  # etapa <nome> <comando...>: roda, imprime o tempo e devolve o código do comando
  local nome="$1"; shift
  local t0; t0=$(date +%s%N)
  echo "=== $nome"
  local codigo=0
  "$@" || codigo=$?
  local ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  printf '=== %s: %d.%03d s (código %d)\n\n' "$nome" $((ms / 1000)) $((ms % 1000)) "$codigo"
  return "$codigo"
}

echo "alvo: $(grep -E '^NEO4J_URI=' "$ENV_FILE" | cut -d= -f2-) (env-file $ENV_FILE)"
echo

EXTRA=()
[[ -d "$CACHE_EXTRA" ]] && EXTRA=(--cache-extra "$CACHE_EXTRA")
etapa "esquema" "$SARC" --env-file "$ENV_FILE" esquema > /dev/null
FALHAS=()
for recorte in "100 2025 3" "104 2025 5" "074 2018 1" "107 2018 0" "123 2020 0"; do
  read -r municipio exercicio restos <<< "$recorte"
  # um caso que falha (ex.: balde faltando) não impede os outros, mas é registrado e derruba o script no fim
  if ! etapa "preterição: $municipio × $exercicio+$restos" "$SARC" --env-file "$ENV_FILE" pretericao \
    --municipio "$municipio" --exercicio "$exercicio" --restos "$restos" "${EXTRA[@]}"; then
    FALHAS+=("$municipio × $exercicio+$restos")
  fi
done
CONFERENCIA=0
etapa "posição de cada credor" "$PY" "$RAIZ/scripts/conferir_pretericao.py" --env-file "$ENV_FILE" || CONFERENCIA=$?
if (( ${#FALHAS[@]} )); then
  echo "FALHOU o cálculo de: ${FALHAS[*]} (veja os baldes faltando acima)" >&2
  exit 1
fi
exit "$CONFERENCIA"
