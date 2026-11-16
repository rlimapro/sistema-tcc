#!/usr/bin/env bash
# Amostra ponta a ponta com dados reais (T08): ETL 1 → ETL 2 → conluio → tool MCP via cliente MCP real.
#
# Uso:
#   scripts/amostra_ponta_a_ponta.sh --env-file <arquivo> [--municipio 087] [--desde 2020] [--ate 2021]
#                                    [--rfb data/rfb/AAAA-MM] [--cache-extra data/raw/tce_sim_v2]
#                                    [--com-rede]
#
# - ETL 1: só do cache (TCE_CACHE_PATH + --cache-extra, este só lido), a menos que se passe --com-rede.
# - ETL 2: em vez de ler os 27 GiB da RFB, faz um recorte: um `grep -F` pelas raízes de CNPJ que o ETL 1
#   gravou, nos 10 arquivos de cada tipo, reunidos em Empresas0/Estabelecimentos0/Socios0 numa pasta
#   temporária. O ETL 2 roda normalmente sobre o recorte (mesmo resultado para esses CNPJs).
# - Os checkpoints dos ETLs ficam na pasta temporária: a amostra não marca arquivo da RFB como concluído
#   para a carga completa.
# - Grava no banco do --env-file (obrigatório; MERGE idempotente). Rode num banco separado (ex.: o
#   neo4j-teste em 7700): no banco principal ficariam cargas `parcial` registradas. Imprime contagens e
#   o tempo de cada etapa.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=""
MUNICIPIO="087"
DESDE="2020"
ATE="2021"
RFB=""
CACHE_EXTRA="$RAIZ/data/raw/tce_sim_v2"
SO_CACHE="--so-cache"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$(realpath "$2")"; shift 2 ;;
    --municipio) MUNICIPIO="$2"; shift 2 ;;
    --desde) DESDE="$2"; shift 2 ;;
    --ate) ATE="$2"; shift 2 ;;
    --rfb) RFB="$(realpath "$2")"; shift 2 ;;
    --cache-extra) CACHE_EXTRA="$(realpath "$2")"; shift 2 ;;
    --com-rede) SO_CACHE=""; shift ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$ENV_FILE" ]]; then
  echo "informe --env-file (de preferência um banco separado, não o principal)" >&2
  exit 2
fi
# --municipio aceita lista (087,144); as consultas de conluio e as tools MCP usam o primeiro.
MUNICIPIO_CONSULTA="${MUNICIPIO%%,*}"

SARC="$RAIZ/.venv/bin/sarc"
PY="$RAIZ/.venv/bin/python"
[[ -x "$SARC" ]] || (cd "$RAIZ" && uv sync --quiet)
# Snapshot da RFB: o indicado em --rfb ou o mais recente de data/rfb (mesma regra do ETL 2; D-40).
RFB="$("$PY" -c 'import sys; from sarc.etl_rfb.snapshot import resolver_snapshot as r; print(r(sys.argv[1])[0].resolve())' "${RFB:-$RAIZ/data/rfb}")" || {
  echo "CSVs da RFB não encontrados: extraia o snapshot (uv run sarc etl-rfb --extrair) ou indique a pasta com --rfb" >&2
  exit 2
}
echo "snapshot da RFB em uso: $(basename "$RFB") ($RFB)"
TMP="$(mktemp -d -t sarc-amostra-XXXXXX)"
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

contar() {
  "$PY" - "$ENV_FILE" <<'PY'
import sys
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver, ler
with abrir_driver(carregar(sys.argv[1])) as d:
    for linha in ler(d, "MATCH (n) UNWIND labels(n) AS r RETURN r, count(*) AS n ORDER BY r") + \
                 ler(d, "MATCH ()-[x]->() RETURN type(x) AS r, count(*) AS n ORDER BY r"):
        print(f"  {linha['r']}: {linha['n']}")
PY
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

ALVO="$(grep -E '^NEO4J_URI=' "$ENV_FILE" | cut -d= -f2- || true)"
echo "alvo: ${ALVO:-bolt://localhost:7699 (padrão)} (env-file $ENV_FILE)"
if [[ -z "$ALVO" || "$ALVO" == *:7699 ]]; then
  echo "AVISO: este é o banco principal do v3; a amostra deixa nele cargas 'parcial'." >&2
  echo "       Prefira o banco de teste (docker compose --profile teste up -d neo4j-teste, porta 7700)." >&2
fi
echo "município $MUNICIPIO, $DESDE-$ATE; RFB $RFB; cache extra $CACHE_EXTRA"
echo

etapa "esquema" "$SARC" --env-file "$ENV_FILE" esquema > /dev/null
EXTRA=()
[[ -d "$CACHE_EXTRA" ]] && EXTRA=(--cache-extra "$CACHE_EXTRA")
etapa "ETL 1 (TCE)" "$SARC" --env-file "$ENV_FILE" etl-tce --municipio "$MUNICIPIO" --desde "$DESDE" --ate "$ATE" \
  $SO_CACHE "${EXTRA[@]}"
etapa "recorte da RFB" recortar_rfb
etapa "ETL 2 (RFB)" "$SARC" --env-file "$ENV_FILE" etl-rfb --dados "$RECORTE" \
  --arquivos Empresas0,Estabelecimentos0,Socios0 --refazer
etapa "contagens do grafo" contar
etapa "conluio (distribuição por ano e confiança)" "$SARC" --env-file "$ENV_FILE" mcp distribuicao \
  --municipio "$MUNICIPIO_CONSULTA"
etapa "tools MCP via cliente stdio" "$PY" "$RAIZ/scripts/cliente_mcp.py" --env-file "$ENV_FILE" \
  --municipio "$MUNICIPIO_CONSULTA" --desde "$DESDE-01-01" --ate "$ATE-12-31"
