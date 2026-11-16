# Indícios de irregularidade em contratações públicas municipais do Ceará

Sistema que integra, em um banco de dados orientado a grafos (Neo4j), os dados de contratações dos 184 municípios do Ceará,
publicados pelo TCE-CE na API de Dados Abertos do SIM, ao quadro societário e aos contatos das empresas, publicados pela
Receita Federal, e identifica três indícios de irregularidade:

- **conluio entre licitantes:** empresas que disputam o mesmo certame ligadas por sócio, *holding*, cadeia societária,
  telefone ou endereço, com o vínculo existente na data do certame;
- **aditivação acima do limite legal:** acréscimos contratuais acima de 25% (50% em reforma de edifício ou equipamento);
- **preterição da ordem cronológica de pagamentos:** credores cujas obrigações foram ultrapassadas por obrigações posteriores
  da mesma fila de pagamentos.

Os resultados são separados em níveis (principal e auxiliar, que nunca se somam) e podem ser consultados em linguagem natural
por um LLM, por meio de um servidor MCP (Model Context Protocol) somente leitura com 14 ferramentas.

O sistema aponta **indícios** para priorizar a análise; não afirma irregularidade nem culpa.

Projeto desenvolvido como Trabalho de Conclusão de Curso. Sem manutenção garantida; *issues* são bem-vindas.

## Requisitos

- Linux, Docker com Compose e [uv](https://docs.astral.sh/uv/) (Python 3.13).
- Para a carga completa: cerca de 40 GB livres e algumas horas (a coleta pela API do TCE-CE é o passo mais demorado).

## Instalação

```bash
git clone <url-do-repositorio> && cd <pasta-do-clone>
uv sync
cp .env.example .env        # preencha ao menos NEO4J_PASSWORD (mínimo de 8 caracteres)
docker compose up -d neo4j  # navegador em http://localhost:7484, Bolt em bolt://localhost:7699
uv run sarc esquema         # constraints e índices
```

## Dados

- **TCE-CE (SIM):** coletados automaticamente pela API (https://api-dados-abertos.tce.ce.gov.br/sim) e guardados em cache em
  `data/raw/tce_sim`.
- **Receita Federal:** baixe os ZIPs de um mês dos Dados Abertos do CNPJ
  (https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9) para `data/rfb/AAAA-MM/`. A extração é feita pelo
  próprio sistema (`uv run sarc etl-rfb --extrair`); sem `RFB_SNAPSHOT`, é usado o mês mais recente da pasta.

## Carga

Em um só comando, para os 184 municípios (sobe o Neo4j, coleta, carrega, calcula os indícios e gera as distribuições em
`resultados/`; os logs ficam em `logs/carga-<data-hora>/`):

```bash
nohup scripts/carga_completa.sh --zerar-banco --workers 6 > logs/carga-nohup.out 2>&1 &
tail -f logs/carga-*/carga.log
```

`--zerar-banco` apaga o banco antes (use na primeira carga). Se terminar com pendências, rode o mesmo comando sem ele: a carga
retoma do cache. Outras opções: `--desde AAAA` (padrão 2009), `--exercicio AAAA` e `--restos N` (janela da preterição),
`--workers N` (1 a 8) e `--municipios 087,144` (recorte; útil para um teste rápido).

As etapas, na ordem em que o script as executa, também podem ser rodadas à mão (`uv run sarc --help` lista os comandos):
`etl-tce` (licitações, contratos, aditivos e execução orçamentária), `pretericao`, `etl-rfb` (quadro societário e contatos),
`etl-rfb --grau-nacional` (grau dos contatos) e `vinculo-aproximado`. Cada etapa é idempotente e pode ser repetida.

## Consulta por um LLM (servidor MCP)

O servidor fala MCP por stdio e só lê o banco (sessões de leitura com tempo-limite de 30 s).

**Claude Code:**

```bash
claude mcp add sarc -- uv --directory /caminho/para/o/repositorio run sarc --env-file .env mcp
```

**Claude Desktop ou outro cliente com configuração JSON:**

```json
{
  "mcpServers": {
    "sarc": {
      "command": "uv",
      "args": ["--directory", "/caminho/para/o/repositorio", "run", "sarc", "--env-file", ".env", "mcp"]
    }
  }
}
```

Teste sem LLM: `uv run python scripts/cliente_mcp.py --env-file .env --municipio 087`.

Ferramentas: `resumo_da_carga`, `cobertura_dos_dados`, `esquema_do_grafo`, `cypher_somente_leitura`, `recusar_pergunta`,
`conluio_na_licitacao`, `licitacoes_com_conluio`, `vinculos_da_empresa`, `aditivacao_do_contrato`,
`contratos_com_aditivacao`, `aditivacao_do_fornecedor`, `ranking_de_preteridos`, `pretericao_do_credor` e
`municipios_com_pretericao`. Toda chamada é registrada em `logs/auditoria/` (configurável por `AUDITORIA_DIR`).

Exemplos de pergunta: "A licitação 2901.01/2021-CP de Itatira tem indício de conluio?", "Quantos contratos de Quixadá estão
com aditivação acima do limite legal?", "Quais são os cinco credores mais preteridos em Maranguape?".

## Testes

```bash
docker compose --profile teste up -d neo4j-teste   # banco de teste efêmero (porta 7700)
uv run pytest                                       # sem o banco de teste, os de integração são pulados
uv run ruff check && uv run ruff format --check
```

Os testes de integração recusam um banco que não esteja vazio; nunca aponte `NEO4J_TEST_URI` para o banco principal.

## Avaliação e benchmark

- `avaliacao_llm/`: as 80 perguntas da avaliação da consulta em linguagem natural e o script que as executa num cliente MCP.
- `benchmark/`: comparação Neo4j × PostgreSQL (`docker compose --profile bench up -d postgres-bench`).
