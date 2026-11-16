#!/usr/bin/env bash
# Conferência dos casos-base da aditivação antes (e depois) da carga completa (D-35).
#
# Uso:
#   scripts/casos_aditivacao.sh --env-file <arquivo> [--cache-extra data/raw/tce_sim_v2]
#
# Carrega do cache só os municípios × anos dos contratos dos casos (Morada Nova 113 × 2019-2024,
# Morrinhos 115 × 2011-2016, Acopiara 005 × 2013-2016, Icó 073 × 2019-2021, Quixadá 144 × 2021-2026) e
# imprime o nível de cada contrato (scripts/conferir_aditivacao.py). Sem RFB: a aditivação não depende dela.
# Use um banco SEPARADO e vazio (ex.: o neo4j-teste em 7700): no principal ficariam cargas parciais.
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

etapa() {  # etapa <nome> <comando...>: roda e imprime o tempo
  local nome="$1"; shift
  local t0; t0=$(date +%s%N)
  echo "=== $nome"
  "$@"
  local ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  printf '=== %s: %d.%03d s\n\n' "$nome" $((ms / 1000)) $((ms % 1000))
}

quieto() {  # quieto <comando...>: roda sem a saída padrão (o tempo da etapa continua visível)
  "$@" > /dev/null
}

echo "alvo: $(grep -E '^NEO4J_URI=' "$ENV_FILE" | cut -d= -f2-) (env-file $ENV_FILE)"
echo

EXTRA=()
[[ -d "$CACHE_EXTRA" ]] && EXTRA=(--cache-extra "$CACHE_EXTRA")
etapa "esquema" quieto "$SARC" --env-file "$ENV_FILE" esquema
for recorte in "113 2019 2024" "115 2011 2016" "005 2013 2016" "073 2019 2021" "144 2021 2026"; do
  read -r municipio desde ate <<< "$recorte"
  etapa "ETL 1: $municipio × $desde-$ate" quieto "$SARC" --env-file "$ENV_FILE" etl-tce --municipio "$municipio" \
    --desde "$desde" --ate "$ate" --so-cache "${EXTRA[@]}"
done
etapa "nível de cada contrato" "$PY" "$RAIZ/scripts/conferir_aditivacao.py" --env-file "$ENV_FILE"
