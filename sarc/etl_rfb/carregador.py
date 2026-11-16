"""Carga das linhas transformadas da RFB no Neo4j (Cypher `MERGE`, idempotente).

Defesa em três camadas contra o enriquecimento de quem não é fornecedor (herdada do v2):
1. o leitor só deixa passar linhas cuja raiz de CNPJ está no grafo;
2. o transformador descarta a filial cujo CNPJ de 14 dígitos não está no grafo;
3. aqui, todo `Estabelecimento` é casado com `MATCH`, nunca criado: o ETL 2 não cria Estabelecimento (quem
   cria são o ETL 1 e, para os credores PJ, a preterição, ambos por `MERGE`; ver `sarc/pretericao/carga.py`).

Modelo (combinado com etl-tce e mcp):
    (:Estabelecimento)-[:PERTENCE_A]->(:Empresa {cnpjBasico, razaoSocial})
    (:Pessoa {id})-[:SOCIO {dataEntrada, qualificacao, snapshot, fonte}]->(:Empresa)
    (:Empresa)-[:SOCIO_PJ {dataEntrada, qualificacao, snapshot, fonte}]->(:Empresa)
    (:Pessoa)-[:REPRESENTA {qualificacao, snapshot, fonte}]->(:Empresa)
    (:Estabelecimento)-[:TEM_TELEFONE|TEM_EMAIL|LOCALIZADO_EM {snapshot, fonte}]->(:Telefone|:Email|:Endereco)

As arestas são chaveadas por `snapshot`: carregar outro mês cria arestas novas, sem apagar as antigas.
"""

from __future__ import annotations

import logging
from typing import Any, LiteralString

from neo4j import Driver

from sarc.grafo.conexao import escrever, escrever_em_lotes, ler

logger = logging.getLogger(__name__)

FONTE = "rfb"

CYPHER_CNPJS_CONHECIDOS: LiteralString = """
MATCH (e:Estabelecimento)
WHERE e.cnpj IS NOT NULL
RETURN e.cnpj AS cnpj
"""

# Empresa e PERTENCE_A para toda raiz já no grafo, mesmo sem ler o arquivo Empresas: assim o
# SOCIO de uma amostra só de `Socios0` não fica sem a empresa de destino.
CYPHER_EMPRESAS_DO_GRAFO: LiteralString = """
MATCH (e:Estabelecimento)
WHERE e.cnpjBasico IS NOT NULL
MERGE (emp:Empresa {cnpjBasico: e.cnpjBasico})
MERGE (e)-[:PERTENCE_A]->(emp)
RETURN count(DISTINCT emp) AS empresas
"""

CYPHER_EMPRESA: LiteralString = """
UNWIND $linhas AS l
MATCH (emp:Empresa {cnpjBasico: l.cnpjBasico})
SET emp.razaoSocial = l.razaoSocial, emp.naturezaJuridica = l.naturezaJuridica, emp.porte = l.porte
"""

CYPHER_ESTABELECIMENTO: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
SET e.situacaoCadastral = l.situacaoCadastral,
    e.dataInicioAtividade = date(l.dataInicioAtividade),
    e.cnaePrincipal = l.cnaePrincipal,
    e.matrizFilial = l.matrizFilial
"""

CYPHER_TELEFONE: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
MERGE (t:Telefone {numero: l.numero})
MERGE (e)-[r:TEM_TELEFONE {snapshot: l.snapshot}]->(t)
SET r.fonte = l.fonte
"""

CYPHER_EMAIL: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
MERGE (m:Email {endereco: l.endereco})
MERGE (e)-[r:TEM_EMAIL {snapshot: l.snapshot}]->(m)
SET r.fonte = l.fonte
"""

CYPHER_ENDERECO: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
MERGE (en:Endereco {id: l.endereco.id})
SET en.cep = l.endereco.cep, en.logradouro = l.endereco.logradouro, en.numero = l.endereco.numero,
    en.bairro = l.endereco.bairro, en.municipio = l.endereco.municipio, en.uf = l.endereco.uf
MERGE (e)-[r:LOCALIZADO_EM {snapshot: l.snapshot}]->(en)
SET r.fonte = l.fonte
"""

# A empresa de destino é casada (MATCH): ela vem de CYPHER_EMPRESAS_DO_GRAFO.
CYPHER_SOCIO: LiteralString = """
UNWIND $linhas AS l
MATCH (emp:Empresa {cnpjBasico: l.cnpjBasico})
MERGE (p:Pessoa {id: l.idPessoa})
MERGE (p)-[r:SOCIO {snapshot: l.snapshot}]->(emp)
SET r.qualificacao = l.qualificacao, r.dataEntrada = date(l.dataEntrada), r.fonte = l.fonte
"""

# A empresa sócia (holding) é criada com MERGE: ela pode não ser fornecedora, e sem o nó a
# aresta não existiria (decisão do v2, mantida).
CYPHER_SOCIO_PJ: LiteralString = """
UNWIND $linhas AS l
MATCH (emp:Empresa {cnpjBasico: l.cnpjBasico})
MERGE (socia:Empresa {cnpjBasico: l.cnpjBasicoSocio})
MERGE (socia)-[r:SOCIO_PJ {snapshot: l.snapshot}]->(emp)
SET r.qualificacao = l.qualificacao, r.dataEntrada = date(l.dataEntrada), r.fonte = l.fonte
"""

CYPHER_REPRESENTA: LiteralString = """
UNWIND $linhas AS l
MATCH (emp:Empresa {cnpjBasico: l.cnpjBasico})
MERGE (p:Pessoa {id: l.idPessoa})
MERGE (p)-[r:REPRESENTA {snapshot: l.snapshot}]->(emp)
SET r.qualificacao = l.qualificacao, r.fonte = l.fonte
"""


def rotulo_snapshot(snapshot: str) -> str:
    """`2026-08` -> `rfb-2026-08` (valor gravado em toda aresta)."""
    return snapshot if snapshot.startswith("rfb-") else f"rfb-{snapshot}"


def buscar_cnpjs_conhecidos(driver: Driver) -> tuple[frozenset[str], frozenset[str]]:
    """(raízes de 8 dígitos, CNPJs de 14) dos Estabelecimentos do grafo. Vazio é erro: rode o ETL 1 antes."""
    cnpjs = frozenset(linha["cnpj"] for linha in ler(driver, CYPHER_CNPJS_CONHECIDOS, timeout=300))
    if not cnpjs:
        raise RuntimeError("nenhum Estabelecimento no grafo: rode `sarc etl-tce` antes de `sarc etl-rfb`")
    return frozenset(c[:8] for c in cnpjs), cnpjs


def garantir_empresas(driver: Driver) -> int:
    return escrever(driver, CYPHER_EMPRESAS_DO_GRAFO)[0]["empresas"]


def _com_origem(linhas: list[dict[str, Any]], snapshot: str) -> list[dict[str, Any]]:
    return [{**linha, "snapshot": snapshot, "fonte": FONTE} for linha in linhas]


def gravar(
    driver: Driver, cypher: LiteralString, linhas: list[dict[str, Any]], snapshot: str, tamanho_lote: int
) -> int:
    """Grava as linhas com `snapshot` e `fonte` acrescentados a cada uma. Devolve o nº de linhas."""
    if not linhas:
        return 0
    return escrever_em_lotes(driver, cypher, _com_origem(linhas, rotulo_snapshot(snapshot)), tamanho_lote)
