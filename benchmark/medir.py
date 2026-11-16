"""Execução e medição das consultas nos dois bancos, e conferência de resultado igual."""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg
from neo4j import Driver
from psycopg.rows import dict_row

from benchmark.consultas import Consulta
from sarc.grafo.conexao import abrir_driver_uri, ler

logger = logging.getLogger(__name__)

Linhas = list[dict[str, Any]]


@dataclass
class Medida:
    banco: str
    consulta: str
    linhas: int = 0
    impressao: str = ""
    quente: list[float] = field(default_factory=list)
    frio: list[float] = field(default_factory=list)
    erro: str | None = None

    @property
    def mediana(self) -> float | None:
        return statistics.median(self.quente) if self.quente else None

    @property
    def p95(self) -> float | None:
        return percentil(self.quente, 95) if self.quente else None

    @property
    def mediana_frio(self) -> float | None:
        return statistics.median(self.frio) if self.frio else None


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo método do posto mais próximo (sem interpolação)."""
    ordenados = sorted(valores)
    posto = max(1, math.ceil(p / 100 * len(ordenados)))
    return ordenados[posto - 1]


def impressao(linhas: Linhas) -> tuple[int, str]:
    """Contagem e hash (SHA-256, 16 hex) das linhas, independentes da ordem e do banco."""
    normalizadas = sorted("\x1f".join(f"{k}={linha[k]}" for k in sorted(linha)) for linha in linhas)
    resumo = hashlib.sha256("\x1e".join(normalizadas).encode("utf-8")).hexdigest()[:16]
    return len(normalizadas), resumo


def executor_neo4j(driver: Driver, consulta: Consulta, params: Mapping[str, Any], timeout: float) -> Callable:
    def _executar() -> Linhas:
        return ler(driver, consulta.cypher, params if consulta.parametrizada else None, timeout=timeout)

    return _executar


def executor_postgres(conexao: psycopg.Connection, consulta: Consulta, params: Mapping[str, Any]) -> Callable:
    def _executar() -> Linhas:
        try:
            with conexao.cursor(row_factory=dict_row) as cursor:
                cursor.execute(consulta.sql, params if consulta.parametrizada else None)
                return cursor.fetchall()
        finally:
            conexao.rollback()

    return _executar


def cronometrar(executar: Callable[[], Linhas]) -> tuple[float, Linhas]:
    inicio = time.perf_counter()
    linhas = executar()
    return time.perf_counter() - inicio, linhas


def medir_quente(medida: Medida, executar: Callable[[], Linhas], aquecimento: int, repeticoes: int) -> None:
    """`aquecimento` execuções descartadas e `repeticoes` medidas (tempo de parede no cliente,
    incluindo a transação e a leitura de todas as linhas)."""
    try:
        for _ in range(aquecimento):
            _, linhas = cronometrar(executar)
        for _ in range(repeticoes):
            segundos, linhas = cronometrar(executar)
            medida.quente.append(segundos)
        if aquecimento + repeticoes == 0:
            _, linhas = cronometrar(executar)
        medida.linhas, medida.impressao = impressao(linhas)
    except Exception as erro:  # noqa: BLE001 - timeout ou erro vira linha do relatório
        medida.erro = f"{type(erro).__name__}: {str(erro)[:200]}"
        logger.warning("%s/%s falhou: %s", medida.banco, medida.consulta, medida.erro)


def reiniciar_containers(containers: list[str], esperar: Callable[[], None]) -> None:
    """Reinicia os containers (cache do banco frio; o cache de páginas do SO continua quente)."""
    subprocess.run(["docker", "restart", *containers], check=True, capture_output=True)
    esperar()


def esperar_neo4j(uri: str, usuario: str, senha: str, limite: float = 180.0) -> None:
    prazo = time.monotonic() + limite
    while True:
        try:
            abrir_driver_uri(uri, usuario, senha).close()
            return
        except Exception:  # noqa: BLE001
            if time.monotonic() > prazo:
                raise
            time.sleep(2)


def esperar_postgres(dsn: str, limite: float = 120.0) -> None:
    prazo = time.monotonic() + limite
    while True:
        try:
            psycopg.connect(dsn, connect_timeout=5).close()
            return
        except psycopg.OperationalError:
            if time.monotonic() > prazo:
                raise
            time.sleep(1)
