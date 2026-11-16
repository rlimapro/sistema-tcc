"""Constraints e índices do grafo do v3. Idempotente: tudo com IF NOT EXISTS.

Nomes de propriedade combinados com os donos dos ETLs. Uma constraint de unicidade
já cria o índice da chave; os índices abaixo são só os de filtro das consultas.
"""

from __future__ import annotations

import logging
from typing import LiteralString

from neo4j import Driver

from sarc.grafo.conexao import escrever, ler

logger = logging.getLogger(__name__)

# (nome, rótulo, propriedade)
CHAVES: list[tuple[str, str, str]] = [
    ("municipio_codigo", "Municipio", "codigo"),
    ("orgao_id", "Orgao", "id"),
    ("licitacao_id", "Licitacao", "id"),
    ("contrato_id", "Contrato", "id"),
    ("empresa_cnpj_basico", "Empresa", "cnpjBasico"),
    ("estabelecimento_cnpj", "Estabelecimento", "cnpj"),
    ("pessoa_id", "Pessoa", "id"),
    ("telefone_numero", "Telefone", "numero"),
    ("email_endereco", "Email", "endereco"),
    ("endereco_id", "Endereco", "id"),
    ("carga_id", "Carga", "id"),
    # Preterição (D-36): só o resultado vai ao grafo, gravado por sarc/pretericao.
    ("fila_pagamento_id", "FilaPagamento", "id"),
    ("resumo_pretericao_id", "ResumoPretericao", "id"),
]

INDICES: list[tuple[str, str, str]] = [
    ("licitacao_data_realizacao", "Licitacao", "dataRealizacao"),
    ("licitacao_municipio", "Licitacao", "municipio"),
    ("estabelecimento_cnpj_basico", "Estabelecimento", "cnpjBasico"),
    # /contratados só conhece município|número|data: casa o Contrato por esta chave não única.
    ("contrato_chave_negociante", "Contrato", "chaveNegociante"),
    # Aditivação e religação de ADITIVA filtram os contratos por município.
    ("contrato_municipio", "Contrato", "municipio"),
    ("fila_pagamento_municipio", "FilaPagamento", "municipio"),
    ("fila_pagamento_janela", "FilaPagamento", "janela"),
]


def comandos() -> list[str]:
    """Os comandos Cypher do esquema, na ordem de aplicação."""
    saida = [
        f"CREATE CONSTRAINT {nome} IF NOT EXISTS FOR (n:{rotulo}) REQUIRE n.{prop} IS UNIQUE"
        for nome, rotulo, prop in CHAVES
    ]
    saida += [
        f"CREATE INDEX {nome} IF NOT EXISTS FOR (n:{rotulo}) ON (n.{prop})" for nome, rotulo, prop in INDICES
    ]
    return saida


def aplicar_esquema(driver: Driver, banco: str | None = None) -> None:
    """Cria constraints e índices que ainda não existem e espera os índices ficarem online."""
    for comando in comandos():
        escrever(driver, comando, banco=banco)
    escrever(driver, "CALL db.awaitIndexes(300)", banco=banco)
    logger.info("esquema aplicado: %d constraints, %d índices", len(CHAVES), len(INDICES))


CONSULTA_CONSTRAINTS: LiteralString = (
    "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
    "RETURN name, labelsOrTypes[0] AS rotulo, properties[0] AS propriedade, type ORDER BY name"
)
CONSULTA_INDICES: LiteralString = (
    "SHOW INDEXES YIELD name, labelsOrTypes, properties, type, owningConstraint, state "
    "WHERE type = 'RANGE' AND owningConstraint IS NULL "
    "RETURN name, labelsOrTypes[0] AS rotulo, properties[0] AS propriedade, state ORDER BY name"
)


def listar_esquema(driver: Driver, banco: str | None = None) -> dict[str, list[dict]]:
    """Constraints e índices (sem os de constraint) presentes no banco."""
    return {
        "constraints": ler(driver, CONSULTA_CONSTRAINTS, banco=banco),
        "indices": ler(driver, CONSULTA_INDICES, banco=banco),
    }
