"""Dados dos testes de aditivação: registros reais de `/contratos` recortados do cache do v2.

`tests/fixtures/aditivacao/contratos.json` tem o original e os aditivos de 8 contratos (58 registros):
os 3 casos-base (Morada Nova, Morrinhos, Acopiara), Icó (confrontado fora do conjunto), os dois de
Quixadá que o v2 declarava como limitação, um `AA` negativo de Banabuiú e uma reforma de unidades
escolares de Carnaubal. As linhas passam pelo transformador do ETL 1 (o mesmo da carga) e ganham
`dataFimVigencia` e `idOriginal`, que a carga grava (D-35).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from neo4j import Driver

from sarc.etl_tce.transformador import transformar_contratos
from sarc.grafo.chaves import id_contrato
from sarc.grafo.conexao import escrever

ARQUIVO = Path(__file__).parents[1] / "fixtures" / "aditivacao" / "contratos.json"

MORADA_NOVA = "113|2090651|2019-11-11"
MORRINHOS = "115|20110143|2011-04-01"
ACOPIARA = "005|20130479|2013-08-12"
ICO = "073|13.02/2019-TP|2019-11-07"
QUIXADA_001 = "144|001/2021|2021-04-07"
QUIXADA_002 = "144|002/2021|2021-04-07"
BANABUIU_NEGATIVO = "022|2025010214|2025-01-02"
CARNAUBAL_REFORMA_ESCOLAR = "040|1103.01/2013|2013-03-11"


def _dia(valor: Any) -> date | None:
    texto = (valor or "")[:10]
    return date.fromisoformat(texto) if texto else None


def brutos() -> list[dict]:
    return json.loads(ARQUIVO.read_text(encoding="utf-8"))


def contratos() -> dict[str, dict]:
    """Linhas como o nó `Contrato` as guarda, por `id`."""
    registros = brutos()
    linhas = {linha["id"]: linha for linha in transformar_contratos(registros, Counter())}
    for r in registros:
        aditivo = r["modalide_contrato"] != "OR"
        chave = id_contrato(
            r["codigo_municipio"],
            r["numero_contrato"],
            r["data_contrato"],
            r["numero_contrato_original"] if aditivo else None,
            r["data_contrato_original"] if aditivo else None,
        )
        linha = linhas[chave]
        linha["dataFimVigencia"] = _dia(r["data_fim_vigencia_contrato"])
        linha["idOriginal"] = (
            id_contrato(r["codigo_municipio"], r["numero_contrato_original"], r["data_contrato_original"])
            if aditivo and (r["numero_contrato_original"] or "").strip()
            else None
        )
    for linha in linhas.values():
        linha["dataContrato"] = _dia(linha["dataContrato"])
    return linhas


def caso(id_original: str) -> tuple[dict, list[dict]]:
    """(original, aditivos) de um contrato da fixture."""
    linhas = contratos()
    aditivos = [linha for linha in linhas.values() if linha["idOriginal"] == id_original]
    return linhas[id_original], aditivos


def gravar(driver: Driver, linhas: list[dict]) -> None:
    """Grava os contratos e a aresta `ADITIVA` como a carga do ETL 1 (D-35): só para original."""
    escrever(
        driver,
        """
        UNWIND $linhas AS l
        MERGE (c:Contrato {id: l.id})
        SET c.chaveNegociante = l.chaveNegociante, c.municipio = l.municipio, c.numero = l.numero,
            c.dataContrato = l.dataContrato, c.dataFimVigencia = l.dataFimVigencia,
            c.modalidade = l.modalidade, c.classe = l.classe, c.valorTotal = l.valorTotal,
            c.valorEspurio = l.valorEspurio, c.valorDivergente = l.valorDivergente,
            c.objeto = l.objeto, c.idOriginal = l.idOriginal
        """,
        {"linhas": linhas},
    )
    escrever(
        driver,
        """
        MATCH (a:Contrato) WHERE a.idOriginal IS NOT NULL
        MATCH (o:Contrato {id: a.idOriginal}) WHERE o.classe = 'original'
        MERGE (a)-[:ADITIVA]->(o)
        """,
    )


def gravar_fixture(driver: Driver) -> None:
    gravar(driver, list(contratos().values()))


def fornecedor(driver: Driver, cnpj: str, contrato: str, razao: str) -> None:
    escrever(
        driver,
        "MATCH (c:Contrato {id: $contrato}) MERGE (e:Estabelecimento {cnpj: $cnpj}) "
        "ON CREATE SET e.cnpjBasico = left($cnpj, 8), e.razaoSocialTce = $razao "
        "MERGE (e)-[:CONTRATADO_EM]->(c)",
        {"cnpj": cnpj, "contrato": contrato, "razao": razao},
    )


def por_original(linhas: dict[str, dict]) -> dict[str, list[dict]]:
    grupos: dict[str, list[dict]] = defaultdict(list)
    for linha in linhas.values():
        if linha["idOriginal"]:
            grupos[linha["idOriginal"]].append(linha)
    return grupos
