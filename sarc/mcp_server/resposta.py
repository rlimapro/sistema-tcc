"""Envelope de toda resposta do servidor MCP: dados, consulta executada, limite de leitura e regime.

Herdado do v2 (`mcp_server/auditoria.py`), com uma correção: o regime vem dos nós `:Carga` gravados
pelos ETLs, e não do `.env` do servidor (CRITICA F-19). Assim o número publicado carrega o escopo
que de fato está no banco.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from neo4j import Driver
from neo4j.time import Date, DateTime, Duration, Time

from sarc.grafo.carga import resumir_cargas


def para_json(valor: Any) -> Any:
    """Converte recursivamente os tipos do driver e do Python para tipos aceitos em JSON."""
    if isinstance(valor, Date | DateTime | Time):
        return valor.iso_format()
    if isinstance(valor, dt.date | dt.datetime | dt.time):
        return valor.isoformat()
    if isinstance(valor, Duration):
        return str(valor)
    if isinstance(valor, dict):
        return {str(k): para_json(v) for k, v in valor.items()}
    if isinstance(valor, list | tuple | set | frozenset):
        return [para_json(v) for v in valor]
    return valor


def regime(driver: Driver, parametros: dict[str, Any] | None = None) -> dict[str, Any]:
    """O que governa o número: o escopo das cargas concluídas no banco e os parâmetros da consulta."""
    cargas = resumir_cargas(driver)
    return {
        "cargas": para_json(cargas),
        "semRegistroDeCarga": not cargas["totalCargas"],
        "parametros": para_json(parametros or {}),
    }


@dataclass
class Resposta:
    """Resposta auditável. `consulta` é o Cypher de fato executado; `limiteDeLeitura` não é opcional."""

    dados: Any
    consulta: str
    limiteDeLeitura: str
    regime: dict[str, Any]
    garantias: dict[str, Any] = field(default_factory=dict)

    def para_dicionario(self) -> dict[str, Any]:
        return para_json(
            {
                "dados": self.dados,
                "consultaExecutada": self.consulta,
                "limiteDeLeitura": self.limiteDeLeitura,
                "garantiasDeMetodo": self.garantias,
                "regime": self.regime,
            }
        )
