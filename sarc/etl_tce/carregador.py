"""Carga das linhas transformadas no Neo4j: `MERGE` idempotente, em lotes, com `execute_write`.

Ordem: municípios, licitações, participações e contato declarado, contratos, `ORIGINADO_DE`,
`ADITIVA` (aditivo -> original, por município), contratados. Rodar duas vezes a mesma carga não
muda nenhuma contagem (teste de integração).
Pessoa física nunca chega aqui: o transformador a descarta e conta.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from typing import LiteralString

from neo4j import Driver

from sarc.grafo.conexao import escrever, escrever_em_lotes, ler

logger = logging.getLogger(__name__)

CYPHER_MUNICIPIO: LiteralString = """
UNWIND $linhas AS l
MERGE (m:Municipio {codigo: l.codigo})
SET m.nome = l.nome, m.codigoIbge = l.codigoIbge
"""

# Colisão entre cargas: a colisão dentro de uma carga é resolvida no transformador; se uma carga
# posterior (outra janela de anos) traz a mesma chave com outra data, a licitação também é marcada
# colidida. A marca é cumulativa, então repetir a mesma carga não a muda.
CYPHER_LICITACAO: LiteralString = """
UNWIND $linhas AS l
MERGE (x:Licitacao {id: l.id})
WITH x, l, coalesce(x.chaveColidida, false)
     OR coalesce(x.dataRealizacao <> date(l.dataRealizacao), false) AS jaColidida
SET x.municipio = l.municipio,
    x.numero = l.numero,
    x.dataRealizacao = date(l.dataRealizacao),
    x.dataSessao = date(l.dataSessao),
    x.modalidade = l.modalidade,
    x.especie = l.especie,
    x.licitacaoDeFato = l.licitacaoDeFato,
    x.nomeOrgaoAta = l.nomeOrgaoAta,
    x.objeto = l.objeto,
    x.valorEstimado = l.valorEstimado,
    x.chaveColidida = l.chaveColidida OR jaColidida,
    x.nRegistrosColididos = l.nRegistrosColididos
WITH x, l
MERGE (m:Municipio {codigo: l.municipio})
MERGE (x)-[:DO_MUNICIPIO]->(m)
"""

# Estabelecimento por MERGE: o licitante perdedor nunca aparece em /contratados. A razão social
# do TCE só na criação: cada prefeitura a digita de um jeito, e com SET vencia o último município.
# A aresta é chaveada pela data do certame: numa chave colidida, cada processo tem a sua.
CYPHER_PARTICIPOU_DE: LiteralString = """
UNWIND $linhas AS l
MATCH (x:Licitacao {id: l.idLicitacao})
MERGE (e:Estabelecimento {cnpj: l.cnpj})
  ON CREATE SET e.cnpjBasico = l.cnpjBasico, e.razaoSocialTce = l.razaoSocial
MERGE (e)-[p:PARTICIPOU_DE {dataRealizacao: date(l.dataRealizacao)}]->(x)
SET p.foneDeclarado = l.foneDeclarado, p.enderecoDeclarado = l.enderecoDeclarado,
    p.cepDeclarado = l.cepDeclarado, p.municipioDeclarado = l.municipioDeclarado
"""

CYPHER_DECLAROU_TELEFONE: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
MERGE (t:Telefone {numero: l.telefone})
MERGE (e)-[r:DECLAROU_CONTATO {licitacao: l.idLicitacao}]->(t)
SET r.fonte = 'tce_505', r.data = date(l.data)
"""

CYPHER_DECLAROU_ENDERECO: LiteralString = """
UNWIND $linhas AS l
MATCH (e:Estabelecimento {cnpj: l.cnpj})
MERGE (a:Endereco {id: l.idEndereco})
  ON CREATE SET a.cep = l.cep, a.numero = l.numero
MERGE (e)-[r:DECLAROU_CONTATO {licitacao: l.idLicitacao}]->(a)
SET r.fonte = 'tce_505', r.data = date(l.data)
"""

CYPHER_CONTRATO: LiteralString = """
UNWIND $linhas AS l
MERGE (c:Contrato {id: l.id})
SET c.chaveNegociante = l.chaveNegociante,
    c.municipio = l.municipio,
    c.numero = l.numero,
    c.dataContrato = date(l.dataContrato),
    c.modalidade = l.modalidade,
    c.classe = l.classe,
    c.valorTotal = l.valorTotal,
    c.valorEspurio = l.valorEspurio,
    c.valorDivergente = l.valorDivergente,
    c.objeto = l.objeto,
    c.tipoObjeto = l.tipoObjeto,
    c.dataFimVigencia = date(l.dataFimVigencia),
    c.idOriginal = l.idOriginal,
    c.numeroProcessoAdm = l.numeroProcessoAdm
"""

# Aditivo -> original, só se o alvo é original (cadeia aditivo -> aditivo não é ligada). Roda por
# município, depois de todos os contratos da carga, sobre TODO aditivo do município ainda sem
# aresta: assim o aditivo carregado numa janela de anos anterior à do original é religado quando o
# original chega (cargas por etapas). Contrato original nunca tem `idOriginal`, então nunca é origem.
# Aditivo de modalidade desconhecida também é ligado; quem consome filtra pela modalidade.
CYPHER_ADITIVA: LiteralString = """
MATCH (a:Contrato {municipio: $municipio})
WHERE a.idOriginal IS NOT NULL AND NOT (a)-[:ADITIVA]->()
MATCH (o:Contrato {id: a.idOriginal})
WHERE o.classe = 'original'
MERGE (a)-[:ADITIVA]->(o)
"""

CYPHER_SITUACAO_ADITIVOS: LiteralString = """
MATCH (a:Contrato {municipio: $municipio})
WHERE a.idOriginal IS NOT NULL
RETURN sum(CASE WHEN (a)-[:ADITIVA]->() THEN 1 ELSE 0 END) AS ligados,
       sum(CASE WHEN (a)-[:ADITIVA]->() THEN 0 ELSE 1 END) AS orfaos
"""

CYPHER_ORIGINADO_DE: LiteralString = """
UNWIND $linhas AS l
MATCH (c:Contrato {id: l.idContrato})
MATCH (x:Licitacao {id: l.idLicitacao})
MERGE (c)-[:ORIGINADO_DE]->(x)
"""

# `chaveNegociante` não é única (irmãos de um lote de aditivos a compartilham): o MATCH pode
# trazer mais de um contrato, de propósito. O Estabelecimento só é criado se o contrato existe.
CYPHER_CONTRATADO_EM: LiteralString = """
UNWIND $linhas AS l
MATCH (c:Contrato {chaveNegociante: l.chaveNegociante})
MERGE (e:Estabelecimento {cnpj: l.cnpj})
  ON CREATE SET e.cnpjBasico = l.cnpjBasico, e.razaoSocialTce = l.razaoSocial
MERGE (e)-[:CONTRATADO_EM]->(c)
"""

CYPHER_LICITACOES_EXISTENTES: LiteralString = """
UNWIND $ids AS id
MATCH (x:Licitacao {id: id})
RETURN x.id AS id
"""


def _gravar(driver: Driver, cypher: LiteralString, linhas: list[dict], rotulo: str, tamanho: int) -> int:
    total = escrever_em_lotes(driver, cypher, linhas, tamanho=tamanho)
    logger.info("gravadas %d linhas de %s", total, rotulo)
    return total


def licitacoes_existentes(driver: Driver, ids: Iterable[str]) -> set[str]:
    lista = sorted(set(ids))
    if not lista:
        return set()
    return {linha["id"] for linha in ler(driver, CYPHER_LICITACOES_EXISTENTES, {"ids": lista}, timeout=120)}


def carregar(
    driver: Driver,
    municipios: list[dict],
    licitacoes: list[dict],
    participacoes: list[dict],
    telefones: list[dict],
    enderecos: list[dict],
    contratos: list[dict],
    originado_de: list[dict],
    contratados: list[dict],
    contagens: Counter[str],
    tamanho_lote: int = 1000,
) -> None:
    """Grava tudo, na ordem que garante que cada MATCH encontre o nó da etapa anterior.

    Linha cuja ponta não existe não é enviada: é contada (`participacao_sem_licitacao`,
    `originado_de_sem_licitacao`, `contratado_sem_contrato`). A participação e o `ORIGINADO_DE`
    conferem a licitação no próprio grafo, porque ela pode ter vindo de uma carga anterior.
    """
    _gravar(driver, CYPHER_MUNICIPIO, municipios, "Municipio", tamanho_lote)
    _gravar(driver, CYPHER_LICITACAO, licitacoes, "Licitacao", tamanho_lote)

    existentes = licitacoes_existentes(
        driver, [p["idLicitacao"] for p in participacoes] + [o["idLicitacao"] for o in originado_de]
    )
    com_licitacao = [p for p in participacoes if p["idLicitacao"] in existentes]
    contagens["participacao_sem_licitacao"] += len(participacoes) - len(com_licitacao)
    contagens["participacoes"] += _gravar(
        driver, CYPHER_PARTICIPOU_DE, com_licitacao, "PARTICIPOU_DE", tamanho_lote
    )
    # O contato só é gerado para licitação de fato da própria carga, que acabou de ser gravada.
    contagens["contatos_telefone"] += _gravar(
        driver, CYPHER_DECLAROU_TELEFONE, telefones, "Telefone", tamanho_lote
    )
    contagens["contatos_endereco"] += _gravar(
        driver, CYPHER_DECLAROU_ENDERECO, enderecos, "Endereco", tamanho_lote
    )

    _gravar(driver, CYPHER_CONTRATO, contratos, "Contrato", tamanho_lote)
    ligaveis = [o for o in originado_de if o["idLicitacao"] in existentes]
    contagens["originado_de_sem_licitacao"] += len(originado_de) - len(ligaveis)
    contagens["originado_de"] += _gravar(driver, CYPHER_ORIGINADO_DE, ligaveis, "ORIGINADO_DE", tamanho_lote)

    for municipio in sorted({c["municipio"] for c in contratos}):
        escrever(driver, CYPHER_ADITIVA, {"municipio": municipio})
        situacao = ler(driver, CYPHER_SITUACAO_ADITIVOS, {"municipio": municipio}, timeout=120)[0]
        # Situação do município no grafo depois desta carga (não o que esta carga criou): repetir a
        # carga dá os mesmos números, e o órfão que a janela seguinte resolve deixa de contar.
        contagens["aditivo_ligado"] += situacao["ligados"] or 0
        contagens["aditivo_orfao"] += situacao["orfaos"] or 0

    chaves_carregadas = {c["chaveNegociante"] for c in contratos}
    com_contrato = [c for c in contratados if c["chaveNegociante"] in chaves_carregadas]
    contagens["contratado_sem_contrato"] += len(contratados) - len(com_contrato)
    contagens["contratados"] += _gravar(
        driver, CYPHER_CONTRATADO_EM, com_contrato, "CONTRATADO_EM", tamanho_lote
    )
