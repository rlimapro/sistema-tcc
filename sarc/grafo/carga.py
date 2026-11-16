"""Nó `:Carga`: o registro de cada execução de ETL, gravado no próprio grafo (CRITICA F-19).

No v2, o servidor MCP declarava o regime (municípios, janela) a partir do `.env`, e
publicou números de uma base com o regime de outra. Aqui cada ETL chama
`registrar_carga()` ao terminar, e o MCP lê o regime do grafo com `resumir_cargas()`.

Propriedades do nó:
- `id` (`<etapa>|<inicio ISO>`), `etapa` (`etl-tce`, `etl-rfb`, ...), `status`, `inicio`, `fim`,
  `duracaoSegundos`, `versaoSarc`;
- os parâmetros conhecidos promovidos a propriedade, para filtrar em Cypher: `municipios` (lista de códigos),
  `desde` e `ate` (anos, inteiros), `snapshotRfb`, `amostra`;
- `parametrosJson` e `contagensJson`: tudo o que veio, em JSON (o Neo4j não guarda mapa como propriedade).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from neo4j import Driver

from sarc.grafo.conexao import escrever, ler

logger = logging.getLogger(__name__)

# chave em `parametros` -> propriedade do nó
PARAMETROS_PROMOVIDOS: dict[str, str] = {
    "municipios": "municipios",
    "desde": "desde",
    "ate": "ate",
    "snapshot_rfb": "snapshotRfb",
    "amostra": "amostra",
}

STATUS_VALIDOS = frozenset({"concluida", "parcial", "incompleta", "falhou"})

CYPHER_REGISTRAR = """
MERGE (c:Carga {id: $id})
SET c += $propriedades
RETURN c.id AS id
"""

CYPHER_LISTAR = """
MATCH (c:Carga)
RETURN c {.*} AS carga
ORDER BY c.inicio DESC
LIMIT $limite
"""


def _versao_sarc() -> str:
    try:
        return version("sarc")
    except PackageNotFoundError:
        return "desconhecida"


def _como_utc(momento: datetime) -> datetime:
    if momento.tzinfo is None:
        raise ValueError("inicio e fim precisam ter fuso horário (use datetime.now(UTC))")
    return momento.astimezone(UTC)


def _promovido(valor: Any) -> Any:
    """Valor aceito como propriedade do Neo4j: escalar ou lista de escalares (o resto vira texto)."""
    if valor is None or isinstance(valor, bool | int | float | str):
        return valor
    if isinstance(valor, list | tuple | set | frozenset):
        itens = sorted(valor) if isinstance(valor, set | frozenset) else list(valor)
        return [v if isinstance(v, bool | int | float | str) else str(v) for v in itens]
    return str(valor)


def registrar_carga(
    driver: Driver,
    etapa: str,
    parametros: Mapping[str, Any],
    contagens: Mapping[str, int],
    inicio: datetime,
    fim: datetime,
    status: str = "concluida",
) -> str:
    """Grava (ou regrava, se o mesmo `id` já existir) o nó `:Carga` da execução. Devolve o `id`.

    `parametros`: o que define o recorte da carga (ex.: `{"municipios": ["087"], "desde": 2020,
    "ate": 2021, "amostra": None}` ou `{"snapshot_rfb": "2026-08", "arquivos": [...]}`).
    `contagens`: números da execução (ex.: `{"licitacoes": 120, "cnpj_invalido_descartado": 3}`).
    `status`: `concluida`; `parcial` (recorte deliberado, ex.: `--arquivos` ou `--max-linhas` do ETL 2);
    `incompleta` (ex.: faltou balde no cache) ou `falhou`. Uma carga não concluída pode ter deixado dados
    no grafo, mas não entra no regime de `resumir_cargas` (só em `cargasNaoConcluidas`).
    """
    if status not in STATUS_VALIDOS:
        raise ValueError(f"status inválido {status!r}; use um de {sorted(STATUS_VALIDOS)}")
    inicio_utc, fim_utc = _como_utc(inicio), _como_utc(fim)
    id_carga = f"{etapa}|{inicio_utc.isoformat()}"
    propriedades: dict[str, Any] = {
        "etapa": etapa,
        "status": status,
        "inicio": inicio_utc,
        "fim": fim_utc,
        "duracaoSegundos": round((fim_utc - inicio_utc).total_seconds(), 3),
        "versaoSarc": _versao_sarc(),
        "parametrosJson": json.dumps(dict(parametros), ensure_ascii=False, sort_keys=True, default=str),
        "contagensJson": json.dumps(dict(contagens), ensure_ascii=False, sort_keys=True),
    }
    for chave, propriedade in PARAMETROS_PROMOVIDOS.items():
        if parametros.get(chave) is not None:
            propriedades[propriedade] = _promovido(parametros[chave])
    escrever(driver, CYPHER_REGISTRAR, {"id": id_carga, "propriedades": propriedades})
    logger.info("carga registrada: %s (%s, %.1f s)", id_carga, status, propriedades["duracaoSegundos"])
    return id_carga


def _legivel(carga: dict[str, Any]) -> dict[str, Any]:
    saida = dict(carga)
    for campo in ("inicio", "fim"):
        if saida.get(campo) is not None:
            saida[campo] = saida[campo].iso_format()
    saida["parametros"] = json.loads(saida.pop("parametrosJson", None) or "{}")
    saida["contagens"] = json.loads(saida.pop("contagensJson", None) or "{}")
    return saida


def listar_cargas(driver: Driver, limite: int = 50) -> list[dict[str, Any]]:
    """As cargas mais recentes primeiro, com datas em ISO e `parametros`/`contagens` como dicionários."""
    return [_legivel(linha["carga"]) for linha in ler(driver, CYPHER_LISTAR, {"limite": limite})]


def resumir_cargas(driver: Driver) -> dict[str, Any]:
    """O regime efetivo do grafo, montado só a partir dos nós `:Carga` concluídos.

    `recortes`: cada recorte distinto do ETL 1 ({municipios, desde, ate, amostra}). É o campo que diz
    qual janela vale para qual município; `municipios` é só a união dos municípios carregados, e
    `envelopeDesde`/`envelopeAte` o menor e o maior ano entre todos os recortes (um município pode ter
    janela menor que o envelope). `snapshotsRfb` vem das cargas concluídas do ETL 2.
    `cargasNaoConcluidas` conta as parciais, incompletas e falhas: elas podem ter deixado dados no grafo.
    """
    cargas = listar_cargas(driver, limite=10_000)
    concluidas = [c for c in cargas if c.get("status") == "concluida"]
    tce = [c for c in concluidas if c.get("etapa") == "etl-tce"]
    rfb = [c for c in concluidas if c.get("etapa") == "etl-rfb"]
    recortes: list[dict[str, Any]] = []
    for c in sorted(tce, key=lambda c: c["inicio"]):
        recorte = {
            "municipios": sorted(c.get("municipios") or []),
            "desde": c.get("desde"),
            "ate": c.get("ate"),
            "amostra": c.get("amostra"),
        }
        if recorte not in recortes:
            recortes.append(recorte)
    desdes = [r["desde"] for r in recortes if r["desde"] is not None]
    ates = [r["ate"] for r in recortes if r["ate"] is not None]
    return {
        "recortes": recortes,
        "municipios": sorted({m for r in recortes for m in r["municipios"]}),
        "envelopeDesde": min(desdes) if desdes else None,
        "envelopeAte": max(ates) if ates else None,
        "snapshotsRfb": sorted({c["snapshotRfb"] for c in rfb if c.get("snapshotRfb")}),
        "cargasComAmostra": sum(1 for c in concluidas if c.get("amostra") is not None),
        "cargasNaoConcluidas": len(cargas) - len(concluidas),
        "ultimaCarga": cargas[0]["fim"] if cargas else None,
        "totalCargas": len(cargas),
    }
