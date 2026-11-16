#!/usr/bin/env bash
# Carga completa do SARC-CE v3 num só comando (D-37, D-40, D-41): todos os municípios do Ceará, os três indícios.
#
# Uso (de dentro de mvp-v3/; para deixar rodando sozinho, ver README.md):
#   scripts/carga_completa.sh [--env-file .env] [--workers 6] [--desde 2009] [--exercicio AAAA] [--restos 3]
#                             [--zerar-banco] [--municipios 087,144]
#
#   --workers N    processos paralelos na coleta pela API (padrão 6; no máximo 8, D-33). A escrita no Neo4j é sequencial.
#   --desde AAAA   primeiro ano do núcleo (conluio e aditivação). Padrão 2009; vai até o ano corrente.
#   --exercicio    último exercício da preterição (padrão: o ano anterior, último fechado).
#   --restos N     exercícios anteriores de restos a pagar na mesma fila (padrão 3).
#   --zerar-banco  APAGA o banco principal (volumes neo4j-data-v3 e neo4j-logs-v3) antes de começar.
#   --municipios   só estes códigos TCE (ensaio ou carga parcial); sem a opção, todos os do Ceará (lista da API).
#
# Etapas, na ordem obrigatória (D-37): verificações → coleta paralela do núcleo → carga do núcleo (ETL 1) →
# coleta paralela da execução orçamentária → preterição → ETL 2 (RFB, snapshot mais recente de RFB_DATA_PATH)
# → grau nacional → vínculo aproximado → distribuições e conferências. Cada etapa é idempotente; rodar o mesmo comando de novo
# retoma do cache e dos checkpoints. Lote ou etapa com balde faltando é repetido uma vez antes de seguir.
# Tudo fica em logs/carga-<data-hora>/ (carga.log é o resumo; um log por etapa e por lote).
set -uo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RAIZ"
ENV_FILE="$RAIZ/.env"
WORKERS=6
DESDE=2009
EXERCICIO=$(( $(date +%Y) - 1 ))
RESTOS=3
ZERAR=0
SELECAO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$(realpath "$2")"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --desde) DESDE="$2"; shift 2 ;;
    --exercicio) EXERCICIO="$2"; shift 2 ;;
    --restos) RESTOS="$2"; shift 2 ;;
    --zerar-banco) ZERAR=1; shift ;;
    --municipios) SELECAO="$2"; shift 2 ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done
if ! [[ "$WORKERS" =~ ^[1-8]$ ]]; then echo "--workers deve ser de 1 a 8 (D-33)" >&2; exit 2; fi

LOGS="$RAIZ/logs/carga-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOGS/lotes"
RESUMO="$LOGS/carga.log"
SARC="$RAIZ/.venv/bin/sarc"
PY="$RAIZ/.venv/bin/python"
FALHAS=()

log() { echo "[$(date '+%d/%m %H:%M:%S')] $*" | tee -a "$RESUMO"; }

duracao() { local s=$1; printf '%dh%02dm%02ds' $((s / 3600)) $((s % 3600 / 60)) $((s % 60)); }

# etapa <nome> <arquivo de log> <comando...>: roda, registra tempo e código de saída; devolve o código.
etapa() {
  local nome="$1" saida="$2"; shift 2
  local t0=$SECONDS codigo=0
  log "INÍCIO  $nome"
  "$@" >> "$saida" 2>&1 || codigo=$?
  log "FIM     $nome: código $codigo em $(duracao $((SECONDS - t0))) (log: ${saida#"$RAIZ"/})"
  return "$codigo"
}

# lotes_em_paralelo <rótulo> <args do etl-tce...>: um processo por lote disjunto; repete uma vez os lotes que falharam.
lotes_em_paralelo() {
  local rotulo="$1"; shift
  local tentativa i pids=() falhou=()
  local -a pendentes=()
  for ((i = 0; i < WORKERS; i++)); do [[ -n "${LOTES[$i]:-}" ]] && pendentes+=("$i"); done
  for tentativa in 1 2; do
    pids=(); falhou=()
    log "$rotulo: tentativa $tentativa com ${#pendentes[@]} lote(s) em paralelo"
    for i in "${pendentes[@]}"; do
      "$SARC" --env-file "$ENV_FILE" etl-tce --municipio "${LOTES[$i]}" "$@" \
        >> "$LOGS/lotes/$rotulo-$i.log" 2>&1 &
      pids+=("$!:$i")
    done
    for par in "${pids[@]}"; do
      wait "${par%%:*}" || falhou+=("${par#*:}")
    done
    if [[ ${#falhou[@]} -eq 0 ]]; then log "$rotulo: todos os lotes terminaram sem balde faltando"; return 0; fi
    log "$rotulo: lote(s) com falha ou balde faltando: ${falhou[*]} (logs em ${LOGS#"$RAIZ"/}/lotes/)"
    pendentes=("${falhou[@]}")
  done
  return 1
}

registrar() {  # registrar <código> <nome>: guarda as etapas que terminaram com erro
  [[ "$1" -eq 0 ]] || FALHAS+=("$2 (código $1)")
}

# avisar_coleta <código da coleta paralela> <código da etapa sequencial seguinte> <nome> <nome da seguinte>:
# a etapa sequencial vai à API buscar o que a coleta paralela deixou faltando; se ela terminou sem balde faltando,
# a falha da coleta paralela foi suprida e vira só aviso (ex.: o servidor derrubou conexões por um tempo).
avisar_coleta() {
  [[ "$1" -eq 0 ]] && return
  if [[ "$2" -eq 0 ]]; then
    log "AVISO: $3 deixou baldes faltando (ver lotes/), completados pela $4 sem nenhum faltando"
  else
    FALHAS+=("$3 (código $1)")
  fi
}

T_INICIO=$SECONDS
log "Carga completa do SARC-CE v3. Logs em ${LOGS#"$RAIZ"/}"
log "Parâmetros: núcleo $DESDE até $(date +%Y); preterição exercício $EXERCICIO + $RESTOS anos de restos ($((EXERCICIO - RESTOS))-$EXERCICIO); $WORKERS processos; env-file $ENV_FILE"

# ---------------------------------------------------------------------------------------------- 0. verificações
[[ -f "$ENV_FILE" ]] || { log "ERRO: $ENV_FILE não existe (copie .env.example e preencha a senha)"; exit 2; }
[[ -x "$SARC" ]] || uv sync --quiet || { log "ERRO: uv sync falhou"; exit 2; }

if [[ "$ZERAR" -eq 1 ]]; then
  log "--zerar-banco: apagando o banco principal (volumes neo4j-data-v3 e neo4j-logs-v3)"
  docker compose down >> "$LOGS/docker.log" 2>&1
  docker volume rm neo4j-data-v3 neo4j-logs-v3 >> "$LOGS/docker.log" 2>&1
  rm -f "$RAIZ"/data/checkpoints/etl-rfb-*.json
fi
docker compose up -d neo4j >> "$LOGS/docker.log" 2>&1 || { log "ERRO: não foi possível subir o Neo4j (ver docker.log)"; exit 2; }
"$PY" - "$ENV_FILE" >> "$LOGS/verificacoes.log" 2>&1 <<'PY' || { log "ERRO: o Neo4j não respondeu em 5 min (ver verificacoes.log)"; exit 2; }
import sys, time
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver, ler
cfg = carregar(sys.argv[1])
for _ in range(60):
    try:
        with abrir_driver(cfg) as d:
            n = ler(d, "MATCH (n) RETURN count(n) AS n")[0]["n"]
        print(f"Neo4j respondeu; nós no banco antes da carga: {n}")
        sys.exit(0)
    except Exception as erro:  # noqa: BLE001
        print(f"aguardando o Neo4j: {type(erro).__name__}")
        time.sleep(5)
sys.exit(1)
PY
log "$(tail -n 1 "$LOGS/verificacoes.log")"

LIVRE_GB=$(df -BG --output=avail "$RAIZ" | tail -1 | tr -dc '0-9')
log "Disco livre: ${LIVRE_GB} GB"
if [[ "$LIVRE_GB" -lt 40 ]]; then log "ERRO: menos de 40 GB livres (RFB extraída ~26 GB, cache ~2 GB, banco)"; exit 2; fi

# Snapshot da RFB: o de RFB_SNAPSHOT ou o mais recente; extrai os ZIPs se for preciso (feito já aqui, para falhar cedo).
"$PY" - "$ENV_FILE" >> "$LOGS/rfb-snapshot.log" 2>&1 <<'PY' || { log "ERRO: snapshot da RFB inválido (ver rfb-snapshot.log)"; exit 2; }
import logging, sys
from sarc.config import carregar
from sarc.etl_rfb.snapshot import extrair_zips, resolver_snapshot
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
cfg = carregar(sys.argv[1])
pasta, rotulo = resolver_snapshot(cfg.rfb_data_path, cfg.rfb_snapshot, exigir_extraido=False)
print(f"extração: {extrair_zips(pasta)}")
pasta, rotulo = resolver_snapshot(pasta, rotulo)
print(f"snapshot da RFB em uso: {rotulo} ({pasta.resolve()})")
PY
log "$(tail -n 1 "$LOGS/rfb-snapshot.log")"

etapa "esquema do grafo" "$LOGS/esquema.log" "$SARC" --env-file "$ENV_FILE" esquema || { log "ERRO: esquema"; exit 2; }

# Lista dos municípios (da API, a mesma regra do `--todos`) em lotes disjuntos, alternados para equilibrar o tamanho.
MUNICIPIOS=$("$PY" - "$ENV_FILE" 2>> "$LOGS/verificacoes.log" <<'PY'
import sys
from sarc.config import carregar
from sarc.etl_tce.cli import _todos_os_municipios
from sarc.etl_tce.extrator import Extrator
c = carregar(sys.argv[1])
e = Extrator(base_url=c.tce_base_url, cache=c.tce_cache_path, tamanho_pagina=c.tce_page_size,
             intervalo=c.tce_request_delay, timeout=c.tce_timeout)
print(" ".join(_todos_os_municipios(e)))
PY
) || { log "ERRO: a API do TCE-CE não devolveu a lista de municípios"; exit 2; }
if [[ -n "$SELECAO" ]]; then
  MUNICIPIOS="${SELECAO//,/ }"
  ALVO_TCE=(--municipio "$SELECAO"); ALVO_PRET=(--municipio "$SELECAO")
else
  ALVO_TCE=(--todos); ALVO_PRET=(--todos-do-cache)
fi
read -r -a TODOS <<< "$MUNICIPIOS"
LOTES=()
for ((i = 0; i < ${#TODOS[@]}; i++)); do
  j=$((i % WORKERS)); LOTES[$j]="${LOTES[$j]:+${LOTES[$j]},}${TODOS[$i]}"
done
log "Municípios: ${#TODOS[@]} em $WORKERS lotes"

# ------------------------------------------------------------------------------------ 1. núcleo (conluio + aditivação)
lotes_em_paralelo "coleta-nucleo" --desde "$DESDE" --sem-carga
COLETA=$?
etapa "carga do núcleo no Neo4j (ETL 1)" "$LOGS/etl-tce-nucleo.log" \
  "$SARC" --env-file "$ENV_FILE" etl-tce "${ALVO_TCE[@]}" --desde "$DESDE" \
  || etapa "carga do núcleo, 2ª tentativa" "$LOGS/etl-tce-nucleo.log" \
       "$SARC" --env-file "$ENV_FILE" etl-tce "${ALVO_TCE[@]}" --desde "$DESDE"
CARGA=$?
registrar "$CARGA" "carga do núcleo (ETL 1)"
avisar_coleta "$COLETA" "$CARGA" "coleta paralela do núcleo" "carga do núcleo"

# ------------------------------------------------------------------------------- 2. execução orçamentária (preterição)
lotes_em_paralelo "coleta-execucao" --bloco execucao --exercicio "$EXERCICIO" --restos "$RESTOS"
COLETA=$?
etapa "conferência da coleta da execução" "$LOGS/etl-tce-execucao.log" \
  "$SARC" --env-file "$ENV_FILE" etl-tce "${ALVO_TCE[@]}" --bloco execucao --exercicio "$EXERCICIO" --restos "$RESTOS"
CONFERENCIA=$?
registrar "$CONFERENCIA" "conferência da coleta da execução"
avisar_coleta "$COLETA" "$CONFERENCIA" "coleta paralela da execução" "conferência da coleta"
grep -h "última remessa" "$LOGS/etl-tce-execucao.log" | tail -n 1 | tee -a "$RESUMO"

etapa "preterição" "$LOGS/pretericao.log" \
  "$SARC" --env-file "$ENV_FILE" pretericao "${ALVO_PRET[@]}" --exercicio "$EXERCICIO" --restos "$RESTOS"
registrar $? "preterição (municípios não calculados estão no log)"
grep -h "não calculados" "$LOGS/pretericao.log" | tail -n 1 | tee -a "$RESUMO"

# --------------------------------------------------------------------------------------------- 3. Receita Federal
etapa "ETL 2 (RFB)" "$LOGS/etl-rfb.log" "$SARC" --env-file "$ENV_FILE" etl-rfb
registrar $? "ETL 2 (RFB)"
grep -h "snapshot da RFB em uso" "$LOGS/etl-rfb.log" | tail -n 1 | tee -a "$RESUMO"
etapa "grau nacional dos contatos" "$LOGS/grau.log" "$SARC" --env-file "$ENV_FILE" etl-rfb --grau-nacional
registrar $? "grau nacional"
etapa "vínculo aproximado (D-47)" "$LOGS/vinculo-aproximado.log" "$SARC" --env-file "$ENV_FILE" vinculo-aproximado
registrar $? "vínculo aproximado"

# ----------------------------------------------------------------------------- 4. distribuições e conferências
mkdir -p "$RAIZ/resultados"
etapa "distribuição do conluio" "$LOGS/distribuicao-conluio.log" \
  bash -c "\"$SARC\" --env-file \"$ENV_FILE\" mcp distribuicao --timeout 3600 > \"$RAIZ/resultados/distribuicao-conluio.json\""
etapa "distribuição da aditivação" "$LOGS/distribuicao-aditivacao.log" \
  bash -c "\"$PY\" \"$RAIZ/scripts/distribuicao_aditivacao.py\" --env-file \"$ENV_FILE\" --timeout 600 > \"$RAIZ/resultados/distribuicao-aditivacao.json\""
etapa "casos-base de conluio no grafo completo" "$RAIZ/resultados/casos-conluio.txt" \
  "$PY" "$RAIZ/scripts/conferir_licitacoes.py" --env-file "$ENV_FILE"
etapa "casos-base de aditivação no grafo completo" "$RAIZ/resultados/casos-aditivacao.txt" \
  "$PY" "$RAIZ/scripts/conferir_aditivacao.py" --env-file "$ENV_FILE"

"$PY" - "$ENV_FILE" >> "$RESUMO" 2>&1 <<'PY'
import sys
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver, ler
with abrir_driver(carregar(sys.argv[1])) as d:
    print("--- conteúdo do grafo ---")
    for linha in ler(d, "MATCH (n) UNWIND labels(n) AS r RETURN r, count(*) AS n ORDER BY r", timeout=600):
        print(f"  {linha['r']}: {linha['n']}")
    print("--- cargas registradas ---")
    for c in ler(d, "MATCH (c:Carga) RETURN c.etapa AS e, c.status AS s ORDER BY c.inicio"):
        print(f"  {c['e']}: {c['s']}")
PY

log "Tempo total: $(duracao $((SECONDS - T_INICIO)))"
if [[ ${#FALHAS[@]} -eq 0 ]]; then
  log "CARGA CONCLUÍDA sem falhas. Resultados em resultados/; resumo em ${RESUMO#"$RAIZ"/}"
  exit 0
fi
log "CARGA TERMINADA COM PENDÊNCIAS: ${FALHAS[*]}"
log "Rode o mesmo comando de novo (sem --zerar-banco): ele retoma do cache e dos checkpoints."
exit 1
