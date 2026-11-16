"""Gravação do resultado da preterição no grafo (só o resultado; D-36).

    (:Orgao {id: 'mun|codigoOrgao', municipio, codigo})-[:DO_MUNICIPIO]->(:Municipio)
    (:FilaPagamento {id: 'janela|mun|orgao|tipoFonte|codigoFonte|categoria', ...})-[:DO_ORGAO]->(:Orgao)
    (:Estabelecimento {cnpj})-[:CREDOR_NA_FILA {janela, componentes, origem, exemplos}]->(:FilaPagamento)
    (:ResumoPretericao {id: 'janela|mun', ...})-[:DO_MUNICIPIO]->(:Municipio)

O `ResumoPretericao` é o que distingue "município calculado" (existe, mesmo com zero obrigações) de "não
calculado" (não existe). Ele guarda o `corte` (a maior data de pagamento observada no município, até
onde a obrigação não paga espera) e, à parte, a última remessa da janela (`ultimaRemessa`): pagamento
remetido depois dela aparece como não pago. Uma reexecução em que o município fica incompleto não grava
nada e, portanto, deixa no grafo o resultado anterior (completo) desse município × janela; a `:Carga` da
reexecução fica `incompleta` e lista o município.

Cada execução completa regrava o município × janela inteiro: apaga as filas e o resumo anteriores e grava
de novo, então um credor que saiu da fila não deixa aresta velha. A janela está nos ids para que duas
janelas nunca se misturem.

Credor PJ entra por MERGE em `Estabelecimento`, como o licitante perdedor no ETL 1: a preterição roda
ANTES do ETL 2, para que a RFB enriqueça também esses credores (ordem fixada na ENTREGA, D-36).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, LiteralString

from neo4j import Driver

from sarc.grafo.conexao import escrever, escrever_em_lotes
from sarc.pretericao.calculo import HEURISTICA, LIGACAO, SEM_PAGAMENTO, Montagem, ResultadoFila

logger = logging.getLogger(__name__)

CYPHER_APAGAR: LiteralString = """
OPTIONAL MATCH (f:FilaPagamento {municipio: $municipio, janela: $janela})
DETACH DELETE f
WITH count(*) AS _
OPTIONAL MATCH (r:ResumoPretericao {id: $idResumo})
DETACH DELETE r
"""

CYPHER_FILAS: LiteralString = """
UNWIND $linhas AS l
MERGE (m:Municipio {codigo: l.municipio})
MERGE (o:Orgao {id: l.idOrgao})
  ON CREATE SET o.municipio = l.municipio, o.codigo = l.orgao
MERGE (o)-[:DO_MUNICIPIO]->(m)
MERGE (f:FilaPagamento {id: l.id})
SET f += l.propriedades
MERGE (f)-[:DO_ORGAO]->(o)
"""

CYPHER_CREDORES: LiteralString = """
UNWIND $linhas AS l
MATCH (f:FilaPagamento {id: l.fila})
MERGE (e:Estabelecimento {cnpj: l.cnpj})
  ON CREATE SET e.cnpjBasico = l.cnpjBasico
MERGE (e)-[r:CREDOR_NA_FILA]->(f)
SET r = l.propriedades
"""

CYPHER_RESUMO: LiteralString = """
MERGE (m:Municipio {codigo: $municipio})
MERGE (r:ResumoPretericao {id: $id})
SET r = $propriedades
MERGE (r)-[:DO_MUNICIPIO]->(m)
"""


def id_orgao(municipio: str, orgao: str) -> str:
    return f"{municipio}|{orgao}"


def id_fila(janela: str, fila: tuple[str, str, str, str, str]) -> str:
    return "|".join((janela, *fila))


def id_resumo(janela: str, municipio: str) -> str:
    return f"{janela}|{municipio}"


def _fracao(parte: int, todo: int) -> float | None:
    return round(parte / todo, 4) if todo else None


def linhas_de_fila(janela: str, corte: str | None, filas: list[ResultadoFila]) -> list[dict[str, Any]]:
    linhas = []
    for f in filas:
        municipio, orgao, tipo_fonte, codigo_fonte, categoria = f.fila
        linhas.append(
            {
                "id": id_fila(janela, f.fila),
                "municipio": municipio,
                "orgao": orgao,
                "idOrgao": id_orgao(municipio, orgao),
                "propriedades": {
                    "janela": janela,
                    "municipio": municipio,
                    "orgao": orgao,
                    "tipoFonte": tipo_fonte,
                    "codigoFonte": codigo_fonte,
                    "categoria": categoria,
                    "corte": corte,
                    "nObrigacoes": f.n_obrigacoes,
                    "nPagas": f.n_pagas,
                    "nCredoresPj": f.n_credores_pj,
                    "nObrigacoesCredorNaoPj": f.n_obrigacoes_credor_nao_pj,
                    "nUltrapassadas": f.n_ultrapassadas,
                    "nPagamentosForaDeOrdem": f.n_pagamentos_fora_de_ordem,
                    "origemLigacao": f.origem[LIGACAO],
                    "origemHeuristica": f.origem[HEURISTICA],
                    "semPagamento": f.origem[SEM_PAGAMENTO],
                },
            }
        )
    return linhas


def linhas_de_credor(janela: str, filas: list[ResultadoFila]) -> list[dict[str, Any]]:
    linhas = []
    for f in filas:
        for c in f.credores:
            linhas.append(
                {
                    "fila": id_fila(janela, f.fila),
                    "cnpj": c.credor,
                    "cnpjBasico": c.credor[:8],
                    "propriedades": {
                        "janela": janela,
                        "nObrigacoes": c.n_obrigacoes,
                        "nPagas": c.n_pagas,
                        "nPagasUltrapassadas": c.n_pagas_ultrapassadas,
                        "ultrapassagensPagas": c.ultrapassagens_pagas,
                        "nNaoPagas": c.n_nao_pagas,
                        "ultrapassagensNaoPagas": c.ultrapassagens_nao_pagas,
                        "maiorEsperaNaoPagaDias": c.maior_espera_nao_paga_dias,
                        "nEsperasLongas": c.n_esperas_longas,
                        "somaEsperasLongasDias": c.soma_esperas_longas_dias,
                        "nPagamentosForaDeOrdem": c.n_pagamentos_fora_de_ordem,
                        "somaAtrasoBeneficiario": c.soma_atraso_beneficiario,
                        "origemLigacao": c.origem[LIGACAO],
                        "origemHeuristica": c.origem[HEURISTICA],
                        "semPagamento": c.origem[SEM_PAGAMENTO],
                        "exemplos": c.exemplos,
                    },
                }
            )
    return linhas


def propriedades_do_resumo(
    municipio: str, janela: str, exercicio: int, restos: int, montagem: Montagem, filas: list[ResultadoFila]
) -> dict[str, Any]:
    n = len(montagem.obrigacoes)
    pagas = sum(f.n_pagas for f in filas)
    fora_de_ordem = sum(f.n_pagamentos_fora_de_ordem for f in filas)
    ultrapassadas = sum(f.n_ultrapassadas for f in filas)
    propriedades: dict[str, Any] = {
        "id": id_resumo(janela, municipio),
        "municipio": municipio,
        "janela": janela,
        "exercicio": exercicio,
        "restos": restos,
        "corte": montagem.corte,
        "totalLiquidacoes": montagem.total_liquidacoes,
        "nObrigacoes": n,
        "nPagas": pagas,
        "nFilas": len(filas),
        "nUltrapassadas": ultrapassadas,
        "nPagamentosForaDeOrdem": fora_de_ordem,
        "fracaoObrigacoesUltrapassadas": _fracao(ultrapassadas, n),
        "fracaoPagamentosForaDeOrdem": _fracao(fora_de_ordem, pagas),
        "origemLigacao": montagem.origem[LIGACAO],
        "origemHeuristica": montagem.origem[HEURISTICA],
        "semPagamento": montagem.origem[SEM_PAGAMENTO],
        "empenhosConflitantes": montagem.empenhos_conflitantes,
        "reconciliacaoBate": montagem.reconciliacao_bate,
    }
    for motivo, quantidade in sorted(montagem.exclusoes.items()):
        propriedades[f"excluidas_{motivo}"] = quantidade
    return propriedades


def gravar_municipio(
    driver: Driver,
    municipio: str,
    janela: str,
    exercicio: int,
    restos: int,
    montagem: Montagem,
    filas: list[ResultadoFila],
    ultima_remessa: str | None = None,
) -> Counter[str]:
    """Regrava o município × janela. Devolve as contagens gravadas."""
    escrever(
        driver,
        CYPHER_APAGAR,
        {"municipio": municipio, "janela": janela, "idResumo": id_resumo(janela, municipio)},
    )
    contagens: Counter[str] = Counter()
    contagens["filas"] = escrever_em_lotes(
        driver, CYPHER_FILAS, linhas_de_fila(janela, montagem.corte, filas)
    )
    contagens["credor_na_fila"] = escrever_em_lotes(driver, CYPHER_CREDORES, linhas_de_credor(janela, filas))
    propriedades = propriedades_do_resumo(municipio, janela, exercicio, restos, montagem, filas)
    propriedades["ultimaRemessa"] = ultima_remessa
    escrever(
        driver,
        CYPHER_RESUMO,
        {"municipio": municipio, "id": propriedades["id"], "propriedades": propriedades},
    )
    contagens["obrigacoes"] = len(montagem.obrigacoes)
    logger.info("preterição gravada: município %s, janela %s, %s", municipio, janela, dict(contagens))
    return contagens
