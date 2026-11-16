#!/usr/bin/env bash
# Conferência dos casos-base antes (e depois) da carga completa (D-32).
#
# Uso:
#   scripts/casos_reais.sh --env-file <arquivo> [--rfb data/rfb/AAAA-MM]
#                          [--cache-extra data/raw/tce_sim_v2]
#
# Carrega só os municípios × anos dos casos (170 × 2009-2010, 098 × 2022, 087 × 2021) a partir do cache,
# recorta a RFB por `grep` pelas raízes de CNPJ do grafo, roda o ETL 2 sobre o recorte, calcula o grau
# nacional com a RFB completa e imprime o nível de cada licitação (scripts/conferir_licitacoes.py).
# Use um banco SEPARADO e vazio (ex.: o neo4j-teste em 7700): no principal ficariam cargas `parcial`.
# Tempo medido em 25/10/2026: cerca de 8 min (recorte ~3 min, grau nacional ~4 min).
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=""
RFB=""
CACHE_EXTRA="$RAIZ/data/raw/tce_sim_v2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$(realpath "$2")"; shift 2 ;;
    --rfb) RFB="$(realpath "$2")"; shift 2 ;;
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
# Snapshot da RFB: o indicado em --rfb ou o mais recente de data/rfb (mesma regra do ETL 2; D-40).
RFB="$("$PY" -c 'import sys; from sarc.etl_rfb.snapshot import resolver_snapshot as r; print(r(sys.argv[1])[0].resolve())' "${RFB:-$RAIZ/data/rfb}")" || {
  echo "CSVs da RFB não encontrados: extraia o snapshot (uv run sarc etl-rfb --extrair) ou indique a pasta com --rfb" >&2
  exit 2
}
echo "snapshot da RFB em uso: $(basename "$RFB") ($RFB)"
TMP="$(mktemp -d -t sarc-casos-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

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

recortar_rfb() {
  "$PY" - "$ENV_FILE" > "$TMP/raizes.txt" <<'PY'
import sys
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver, ler
with abrir_driver(carregar(sys.argv[1])) as d:
    for linha in ler(d, "MATCH (e:Estabelecimento) RETURN DISTINCT left(e.cnpj, 8) AS r ORDER BY r", timeout=300):
        print(f'"{linha["r"]}";')
PY
  echo "  raízes de CNPJ no grafo: $(wc -l < "$TMP/raizes.txt")"
  local destino="$TMP/rfb/$(basename "$RFB")"
  for tipo in Empresas Estabelecimentos Socios; do
    mkdir -p "$destino/${tipo}0"
    LC_ALL=C grep -a -h -F -f "$TMP/raizes.txt" "$RFB/$tipo"[0-9]/* > "$destino/${tipo}0/recorte.csv" || true
    echo "  $tipo: $(wc -l < "$destino/${tipo}0/recorte.csv") linhas no recorte"
  done
  mkdir -p "$destino/Municipios" && cp "$RFB"/Municipios/* "$destino/Municipios/"
  RECORTE="$destino"
}

echo "alvo: $(grep -E '^NEO4J_URI=' "$ENV_FILE" | cut -d= -f2-) (env-file $ENV_FILE)"
echo

EXTRA=()
[[ -d "$CACHE_EXTRA" ]] && EXTRA=(--cache-extra "$CACHE_EXTRA")
etapa "esquema" quieto "$SARC" --env-file "$ENV_FILE" esquema
etapa "ETL 1: 170 × 2009-2010" quieto "$SARC" --env-file "$ENV_FILE" etl-tce --municipio 170 --desde 2009 \
  --ate 2010 --so-cache "${EXTRA[@]}"
etapa "ETL 1: 098 × 2022" quieto "$SARC" --env-file "$ENV_FILE" etl-tce --municipio 098 --desde 2022 --ate 2022 \
  --so-cache "${EXTRA[@]}"
etapa "ETL 1: 087 × 2021" quieto "$SARC" --env-file "$ENV_FILE" etl-tce --municipio 087 --desde 2021 --ate 2021 \
  --so-cache "${EXTRA[@]}"
etapa "recorte da RFB" recortar_rfb
etapa "ETL 2 (RFB)" quieto "$SARC" --env-file "$ENV_FILE" etl-rfb --dados "$RECORTE" \
  --arquivos Empresas0,Estabelecimentos0,Socios0 --refazer
etapa "grau nacional (RFB completa)" "$SARC" --env-file "$ENV_FILE" etl-rfb --dados "$RFB" --grau-nacional
etapa "nível de cada licitação" "$PY" "$RAIZ/scripts/conferir_licitacoes.py" --env-file "$ENV_FILE"
