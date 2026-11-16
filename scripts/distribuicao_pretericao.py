"""Distribuição da preterição por município, em JSON (D-36, D-42).

Uso:
    uv run python scripts/distribuicao_pretericao.py --env-file .env [--janela 2025+3]

Somente leitura: percorre os `ResumoPretericao` gravados e, para cada município, lê as listas pelas
mesmas consultas das tools do MCP (`sarc.pretericao.consultas`). Os dois componentes do credor
continuam separados e nunca são somados (D-36): o principal (obrigações pagas depois de outra
posterior da mesma fila) e o auxiliar (obrigações não pagas até o corte). Credor abaixo do piso de
obrigações é contado, não listado. Janelas diferentes não se somam: sem --janela, exige que haja uma só.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver
from sarc.pretericao.consultas import PISO_OBRIGACOES, pretericao_no_municipio, resumos

CAMPOS_SOMADOS = (
    "nObrigacoes",
    "nPagas",
    "semPagamento",
    "nUltrapassadas",
    "nPagamentosForaDeOrdem",
    "origemLigacao",
    "origemHeuristica",
    "nFilas",
    "totalLiquidacoes",
)


def _fracao(parte: int, todo: int) -> float | None:
    return round(parte / todo, 4) if todo else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--janela", default=None, help="ex.: 2025+3 (exercício + anos de restos a pagar)")
    parser.add_argument("--timeout", type=float, default=120.0, help="por consulta, em segundos")
    args = parser.parse_args()
    with abrir_driver(carregar(args.env_file)) as driver:
        todos = resumos(driver, None, args.timeout)
        janelas = sorted({r["janela"] for r in todos})
        janela = args.janela or (janelas[0] if len(janelas) == 1 else None)
        if janela is None:
            raise SystemExit(f"há mais de uma janela calculada ({janelas}); escolha uma com --janela")
        escolhidos = sorted((r for r in todos if r["janela"] == janela), key=lambda r: r["municipio"])
        total = Counter()
        credores = Counter()
        por_municipio = []
        for r in escolhidos:
            m = pretericao_no_municipio(
                driver, r["municipio"], janela, limite=1_000_000, timeout=args.timeout
            )
            ranking = m["rankingPrincipal"]
            com_ultrapassagem = sum(1 for c in ranking if c["ultrapassagensPagas"] > 0)
            for campo in CAMPOS_SOMADOS:
                total[campo] += r.get(campo) or 0
            credores["acimaDoPiso"] += m["credoresAcimaDoPiso"]
            credores["abaixoDoPiso"] += m["credoresAbaixoDoPiso"]
            credores["comUltrapassagemPaga"] += com_ultrapassagem
            credores["comNaoPagas"] += m["credoresComNaoPagas"]
            total["reconciliacaoNaoBate"] += 0 if r.get("reconciliacaoBate") else 1
            por_municipio.append(
                {
                    "municipio": r["municipio"],
                    "corte": r.get("corte"),
                    "ultimaRemessa": r.get("ultimaRemessa"),
                    "nObrigacoes": r.get("nObrigacoes"),
                    "nPagas": r.get("nPagas"),
                    "fracaoObrigacoesUltrapassadas": r.get("fracaoObrigacoesUltrapassadas"),
                    "fracaoPagamentosForaDeOrdem": r.get("fracaoPagamentosForaDeOrdem"),
                    "fracaoDataPorHeuristica": _fracao(r.get("origemHeuristica") or 0, r.get("nPagas") or 0),
                    "credoresAcimaDoPiso": m["credoresAcimaDoPiso"],
                    "credoresComUltrapassagemPaga": com_ultrapassagem,
                    "credoresComNaoPagas": m["credoresComNaoPagas"],
                    "reconciliacaoBate": r.get("reconciliacaoBate"),
                }
            )
    fracoes = sorted(
        x["fracaoPagamentosForaDeOrdem"]
        for x in por_municipio
        if x["fracaoPagamentosForaDeOrdem"] is not None
    )
    saida = {
        "escopo": {
            "janela": janela,
            "municipios": len(escolhidos),
            "pisoObrigacoes": PISO_OBRIGACOES,
            "nota": "componentes principal (pagas ultrapassadas) e auxiliar (não pagas) nunca somados (D-36)",
        },
        "contagens": {
            **dict(total),
            "fracaoObrigacoesUltrapassadas": _fracao(total["nUltrapassadas"], total["nObrigacoes"]),
            "fracaoPagamentosForaDeOrdem": _fracao(total["nPagamentosForaDeOrdem"], total["nPagas"]),
            "fracaoDataPorHeuristica": _fracao(total["origemHeuristica"], total["nPagas"]),
            "credores": dict(credores),
        },
        "fracaoPagamentosForaDeOrdemPorMunicipio": {
            "minimo": fracoes[0] if fracoes else None,
            "mediana": fracoes[len(fracoes) // 2] if fracoes else None,
            "maximo": fracoes[-1] if fracoes else None,
        },
        "porMunicipio": por_municipio,
    }
    print(json.dumps(saida, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
