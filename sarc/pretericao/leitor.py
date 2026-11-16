"""Leitura do cache dos 4 endpoints da execução orçamentária de um município, numa janela de exercícios.

Não há regra própria de cache aqui: a leitura passa pelo `Extrator` do ETL 1 em modo só-cache
(`sarc.etl_tce.extrator.Extrator.obter_execucao`, da `ingestao`), que já sabe:
- o balde (município, exercício `AAAA00`, mês de remessa `AAAAMM`) e os meses esperados por endpoint
  (`meses_de_remessa`: empenhos 12 meses, liquidações, pagamentos e ligação 24, a ligação só desde 2025-01);
- a precedência do cache principal (`.json.gz`, coletado com `$orderby`) sobre o cache extra do v2;
- a recusa de balde com registro completo repetido (sintoma da paginação sem ordem do v2, D-36).

Balde esperado e ausente ou recusado fica em `faltando`: o município não é calculado (falha fechada). A
leitura projeta cada registro nos campos usados logo ao ler, para caber em memória num município grande.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sarc.etl_tce.extrator import (
    LIQUIDACOES,
    NOTAS_EMPENHOS,
    NOTAS_PAGAMENTOS,
    PAGAMENTOS_LIQUIDACOES,
    Extrator,
    meses_de_remessa,
)
from sarc.pretericao.calculo import Chave, Empenho, Ligacao, Liquidacao, Pagamento

logger = logging.getLogger(__name__)

ENDPOINTS = (NOTAS_EMPENHOS, LIQUIDACOES, NOTAS_PAGAMENTOS, PAGAMENTOS_LIQUIDACOES)


@dataclass(frozen=True)
class Janela:
    """Exercício de referência E (o último fechado) e quantos exercícios anteriores de restos a pagar."""

    exercicio: int
    restos: int

    @property
    def exercicios(self) -> list[int]:
        return list(range(self.exercicio - self.restos, self.exercicio + 1))

    @property
    def id(self) -> str:
        return f"{self.exercicio}+{self.restos}"


@dataclass
class Leitura:
    municipio: str
    janela: Janela
    empenhos: list[Empenho] = field(default_factory=list)
    liquidacoes: list[Liquidacao] = field(default_factory=list)
    pagamentos: list[Pagamento] = field(default_factory=list)
    ligacoes: list[Ligacao] = field(default_factory=list)
    baldes_lidos: Counter[str] = field(default_factory=Counter)
    faltando: list[str] = field(default_factory=list)

    @property
    def completa(self) -> bool:
        return not self.faltando


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor).strip()


def _dia(valor: Any) -> str:
    return _texto(valor)[:10]


def _numero(valor: Any) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _chave(r: dict[str, Any], campo_numero: str = "numero_empenho") -> Chave:
    return (
        _texto(r.get("codigo_municipio")),
        _texto(r.get("exercicio_orcamento")),
        _texto(r.get("codigo_orgao")),
        _texto(r.get(campo_numero)),
    )


def _verdadeiro(valor: Any) -> bool:
    return _texto(valor).lower() in {"true", "1", "s", "sim"}


def projetar_empenho(r: dict[str, Any]) -> Empenho:
    return Empenho(
        chave=_chave(r),
        elemento_despesa=_texto(r.get("codigo_elemento_despesa")),
        subelemento=_texto(r.get("cd_subelem_desp")),
        tipo_fonte=_texto(r.get("tipo_fonte")),
        codigo_fonte=_texto(r.get("codigo_fonte")),
        credor=_texto(r.get("numero_documento_negociante")),
        tipo_credor=_texto(r.get("codigo_tipo_negociante")),
        data_emissao=_dia(r.get("data_emissao_empenho")),
    )


def projetar_liquidacao(r: dict[str, Any]) -> Liquidacao:
    return Liquidacao(
        chave=_chave(r),
        data=_dia(r.get("data_liquidacao")),
        valor=_numero(r.get("valor_liquidado")),
        estornada=_verdadeiro(r.get("status_estornado")),
    )


def projetar_pagamento(r: dict[str, Any]) -> Pagamento:
    return Pagamento(
        chave=_chave(r),
        numero=_texto(r.get("numero_nota_pagamento")),
        data=_dia(r.get("data_nota_pagamento")),
        valor=_numero(r.get("valor_nota_pagamento")),
        estornado=_texto(r.get("estado_de_estornado") or "0") not in {"0", "false", "False"},
    )


def projetar_ligacao(r: dict[str, Any]) -> Ligacao:
    return Ligacao(
        chave=_chave(r, "numero_nota_empenho"),
        numero_pagamento=_texto(r.get("numero_nota_pagamento")),
        data_liquidacao=_dia(r.get("data_liquidacao")),
    )


PROJECOES: dict[str, Callable[[dict[str, Any]], Any]] = {
    NOTAS_EMPENHOS: projetar_empenho,
    LIQUIDACOES: projetar_liquidacao,
    NOTAS_PAGAMENTOS: projetar_pagamento,
    PAGAMENTOS_LIQUIDACOES: projetar_ligacao,
}


def extrator_so_cache(cache: Path, cache_extra: list[Path], hoje: date | None = None) -> Extrator:
    """`Extrator` que nunca usa a rede: o que não está no cache fica faltando."""
    dia = hoje or date.today()
    return Extrator("https://sem-rede.invalid", cache, cache_extra, so_cache=True, hoje=lambda: dia)


def ler_municipio(municipio: str, janela: Janela, extrator: Extrator, hoje: date | None = None) -> Leitura:
    """Lê todos os baldes esperados do município na janela, pelo extrator (em geral só-cache)."""
    hoje = hoje or date.today()
    leitura = Leitura(municipio, janela)
    destinos: dict[str, list[Any]] = {
        NOTAS_EMPENHOS: leitura.empenhos,
        LIQUIDACOES: leitura.liquidacoes,
        NOTAS_PAGAMENTOS: leitura.pagamentos,
        PAGAMENTOS_LIQUIDACOES: leitura.ligacoes,
    }
    for endpoint in ENDPOINTS:
        for exercicio in janela.exercicios:
            for mes in meses_de_remessa(endpoint, exercicio, hoje):
                registros = extrator.obter_execucao(endpoint, municipio, exercicio, mes)
                if registros is None:
                    leitura.faltando.append(f"{endpoint}/{municipio}/{exercicio}00_{mes}")
                    continue
                leitura.baldes_lidos[endpoint] += 1
                destinos[endpoint].extend(PROJECOES[endpoint](r) for r in registros)
    if leitura.faltando:
        logger.warning(
            "município %s, janela %s: %d baldes faltando", municipio, janela.id, len(leitura.faltando)
        )
    return leitura
