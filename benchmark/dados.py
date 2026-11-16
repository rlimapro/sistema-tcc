"""Exporta o subgrafo de conluio do Neo4j para CSV e carrega os CSVs no PostgreSQL.

Só leitura no Neo4j (`ler()`). No PostgreSQL, as tabelas do benchmark são recriadas
a cada execução (DROP + CREATE), com B-tree nas chaves de junção.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from neo4j import Driver

from sarc.grafo.conexao import ler

logger = logging.getLogger(__name__)

TIMEOUT_EXPORTACAO = 600.0


@dataclass(frozen=True)
class Tabela:
    nome: str
    colunas: tuple[str, ...]
    ddl: str
    cypher: str


TABELAS: list[Tabela] = [
    Tabela(
        "licitacao",
        ("id", "data_realizacao"),
        "CREATE TABLE licitacao (id text PRIMARY KEY, data_realizacao date)",
        "MATCH (l:Licitacao) RETURN l.id AS id, toString(l.dataRealizacao) AS data_realizacao",
    ),
    Tabela(
        "estabelecimento",
        ("cnpj", "cnpj_basico"),
        "CREATE TABLE estabelecimento (cnpj text PRIMARY KEY, cnpj_basico text)",
        # cnpj_basico vem da aresta PERTENCE_A (nulo se o ETL 2 não achou a empresa).
        "MATCH (e:Estabelecimento) OPTIONAL MATCH (e)-[:PERTENCE_A]->(emp:Empresa) "
        "RETURN e.cnpj AS cnpj, emp.cnpjBasico AS cnpj_basico",
    ),
    Tabela(
        "participou_de",
        ("cnpj", "licitacao_id"),
        "CREATE TABLE participou_de (cnpj text NOT NULL, licitacao_id text NOT NULL)",
        "MATCH (e:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao) "
        "RETURN e.cnpj AS cnpj, l.id AS licitacao_id",
    ),
    Tabela(
        "socio",
        ("pessoa_id", "cnpj_basico", "data_entrada"),
        "CREATE TABLE socio (pessoa_id text NOT NULL, cnpj_basico text NOT NULL, data_entrada date)",
        "MATCH (p:Pessoa)-[s:SOCIO]->(emp:Empresa) "
        "RETURN p.id AS pessoa_id, emp.cnpjBasico AS cnpj_basico, toString(s.dataEntrada) AS data_entrada",
    ),
    Tabela(
        "socio_pj",
        ("socio_cnpj_basico", "cnpj_basico", "data_entrada"),
        "CREATE TABLE socio_pj "
        "(socio_cnpj_basico text NOT NULL, cnpj_basico text NOT NULL, data_entrada date)",
        "MATCH (socia:Empresa)-[s:SOCIO_PJ]->(emp:Empresa) RETURN socia.cnpjBasico AS socio_cnpj_basico, "
        "emp.cnpjBasico AS cnpj_basico, toString(s.dataEntrada) AS data_entrada",
    ),
    Tabela(
        "tem_telefone",
        ("cnpj", "numero"),
        "CREATE TABLE tem_telefone (cnpj text NOT NULL, numero text NOT NULL)",
        "MATCH (e:Estabelecimento)-[:TEM_TELEFONE]->(t:Telefone) RETURN e.cnpj AS cnpj, t.numero AS numero",
    ),
    Tabela(
        "tem_email",
        ("cnpj", "endereco"),
        "CREATE TABLE tem_email (cnpj text NOT NULL, endereco text NOT NULL)",
        "MATCH (e:Estabelecimento)-[:TEM_EMAIL]->(m:Email) RETURN e.cnpj AS cnpj, m.endereco AS endereco",
    ),
    Tabela(
        "localizado_em",
        ("cnpj", "endereco_id"),
        "CREATE TABLE localizado_em (cnpj text NOT NULL, endereco_id text NOT NULL)",
        "MATCH (e:Estabelecimento)-[:LOCALIZADO_EM]->(en:Endereco) "
        "RETURN e.cnpj AS cnpj, en.id AS endereco_id",
    ),
]

# B-tree nas chaves de junção (as PK já têm índice).
INDICES_SQL: list[str] = [
    "CREATE INDEX ON estabelecimento (cnpj_basico)",
    "CREATE INDEX ON participou_de (licitacao_id, cnpj)",
    "CREATE INDEX ON participou_de (cnpj)",
    "CREATE INDEX ON socio (cnpj_basico, pessoa_id)",
    "CREATE INDEX ON socio (pessoa_id, cnpj_basico)",
    "CREATE INDEX ON socio_pj (cnpj_basico)",
    "CREATE INDEX ON socio_pj (socio_cnpj_basico)",
    "CREATE INDEX ON tem_telefone (cnpj, numero)",
    "CREATE INDEX ON tem_telefone (numero)",
    "CREATE INDEX ON tem_email (cnpj, endereco)",
    "CREATE INDEX ON tem_email (endereco)",
    "CREATE INDEX ON localizado_em (cnpj, endereco_id)",
    "CREATE INDEX ON localizado_em (endereco_id)",
]

# Arestas societárias nos dois sentidos, com nós prefixados (P = pessoa, E = empresa), para a CTE da Q4.
# É o equivalente relacional de percorrer SOCIO|SOCIO_PJ sem direção.
SQL_ARESTA_SOCIETARIA = """
CREATE TABLE aresta_societaria AS
SELECT 'P' || pessoa_id AS origem_no, 'E' || cnpj_basico AS destino_no, data_entrada FROM socio
UNION ALL SELECT 'E' || cnpj_basico, 'P' || pessoa_id, data_entrada FROM socio
UNION ALL SELECT 'E' || socio_cnpj_basico, 'E' || cnpj_basico, data_entrada FROM socio_pj
UNION ALL SELECT 'E' || cnpj_basico, 'E' || socio_cnpj_basico, data_entrada FROM socio_pj;
CREATE INDEX ON aresta_societaria (origem_no, data_entrada);
"""


def exportar_csv(driver: Driver, pasta: Path) -> dict[str, int]:
    """Grava um CSV por tabela em `pasta` e devolve o nº de linhas de cada uma."""
    pasta.mkdir(parents=True, exist_ok=True)
    volumes: dict[str, int] = {}
    for tabela in TABELAS:
        linhas = ler(driver, tabela.cypher, timeout=TIMEOUT_EXPORTACAO)
        with (pasta / f"{tabela.nome}.csv").open("w", newline="", encoding="utf-8") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=tabela.colunas)
            escritor.writeheader()
            escritor.writerows(linhas)
        volumes[tabela.nome] = len(linhas)
        logger.info("exportado %s: %d linhas", tabela.nome, len(linhas))
    return volumes


def carregar_postgres(conexao: psycopg.Connection, pasta: Path) -> None:
    """Recria as tabelas do benchmark e carrega os CSVs com COPY; depois cria índices e roda ANALYZE."""
    with conexao.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS aresta_societaria")
        for tabela in TABELAS:
            cursor.execute(f"DROP TABLE IF EXISTS {tabela.nome}")
            cursor.execute(tabela.ddl)
            colunas = ", ".join(tabela.colunas)
            # CSV sem aspas em campo vazio = NULL (padrão do COPY csv).
            with (
                (pasta / f"{tabela.nome}.csv").open("rb") as arquivo,
                cursor.copy(
                    f"COPY {tabela.nome} ({colunas}) FROM STDIN WITH (FORMAT csv, HEADER true)"
                ) as copia,
            ):
                while bloco := arquivo.read(1 << 20):
                    copia.write(bloco)
        for indice in INDICES_SQL:
            cursor.execute(indice)
        cursor.execute(SQL_ARESTA_SOCIETARIA)
    conexao.commit()
    conexao.autocommit = True
    try:
        conexao.execute("ANALYZE")
    finally:
        conexao.autocommit = False
    logger.info("PostgreSQL carregado a partir de %s", pasta)


ROTULOS = ("Licitacao", "Estabelecimento", "Empresa", "Pessoa", "Telefone", "Email", "Endereco")
TIPOS = ("PARTICIPOU_DE", "PERTENCE_A", "SOCIO", "SOCIO_PJ", "TEM_TELEFONE", "TEM_EMAIL", "LOCALIZADO_EM")


def contar_grafo(driver: Driver) -> dict[str, int]:
    """Nós e arestas do subgrafo usado (contagens do count store), para a tabela de volume do relatório."""
    volumes: dict[str, int] = {}
    for rotulo in ROTULOS:
        volumes[rotulo] = ler(driver, f"MATCH (n:{rotulo}) RETURN count(n) AS n")[0]["n"]
    for tipo in TIPOS:
        volumes[tipo] = ler(driver, f"MATCH ()-[r:{tipo}]->() RETURN count(r) AS n")[0]["n"]
    return volumes
