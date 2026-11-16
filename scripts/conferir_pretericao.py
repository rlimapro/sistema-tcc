"""Confere, no grafo, a posição dos credores dos casos-base de preterição nos dois componentes (D-36).

Uso (depois de `scripts/casos_pretericao.sh` gravar as janelas dos casos, ou sobre o grafo da carga):
    uv run python scripts/conferir_pretericao.py --env-file <arquivo>

Lê pelas mesmas consultas das tools do MCP (`sarc.pretericao.consultas`). Para cada caso imprime as
obrigações do credor, os dois componentes (principal: pagas ultrapassadas; auxiliar: não pagas até o
corte), a posição no ranking principal e na lista auxiliar do município e a origem das datas. Sai com
código 1 se o município não foi calculado, se a reconciliação não fecha, se o credor sumiu ou se o nº
de obrigações do credor mudou. As posições são impressas, não exigidas: são retrato da janela.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver
from sarc.pretericao.consultas import pretericao_do_credor, pretericao_no_municipio


@dataclass(frozen=True)
class Caso:
    nome: str
    municipio: str
    janela: str
    cnpj: str
    obrigacoes: int  # esperado: código de produção sobre os meses recoletados (25/10/2026)
    conjunto_base: bool


CASOS: list[Caso] = [
    Caso("Maranguape, LOCMED (TCE 31953/2023-5)", "100", "2025+3", "04238951000154", 95, True),
    Caso("Mauriti, Panorama (TCE 05349/2025-6)", "104", "2025+5", "01722296000117", 12, True),
    Caso("Iguatu, HF Pneus (TCE 25037/2019-8)", "074", "2018+1", "18180450000179", 13, True),
    Caso("Milhã, HF Pneus (controle fora do conjunto)", "107", "2018+0", "18180450000179", 17, False),
    Caso("Pacatuba (controle fora do conjunto)", "123", "2020+0", "04420916000151", 12, False),
]


def _posicao(lista: list[dict], cnpj: str, total: int) -> str:
    for n, c in enumerate(lista, start=1):
        if c["cnpj"] == cnpj:
            return f"{n}ª de {total} (p. {100 * (1 - (n - 1) / total):.1f})"
    return "fora da lista"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    config = carregar(args.env_file)
    falhas = 0
    with abrir_driver(config) as driver:
        for caso in CASOS:
            print(f"== {caso.nome}: município {caso.municipio}, janela {caso.janela}")
            m = pretericao_no_municipio(driver, caso.municipio, caso.janela, limite=10_000)
            if not m.get("calculado") or "resumo" not in m:
                print(f"   FALHA: {m.get('motivo')}")
                falhas += 1
                continue
            r = m["resumo"]
            print(
                f"   município: {r['nObrigacoes']} obrigações, corte {r['corte']}, "
                f"ultrapassadas {r['fracaoObrigacoesUltrapassadas']}, "
                f"fora de ordem {r['fracaoPagamentosForaDeOrdem']}, origem ligação/heurística/sem = "
                f"{r['origemLigacao']}/{r['origemHeuristica']}/{r['semPagamento']}, "
                f"reconciliação {r['reconciliacaoBate']}"
            )
            filas = [f for f in pretericao_do_credor(driver, caso.cnpj)["filas"]
                     if f["municipio"] == caso.municipio and f["janela"] == caso.janela]  # fmt: skip
            if not r["reconciliacaoBate"] or not filas:
                print("   FALHA: reconciliação não fecha ou credor ausente das filas")
                falhas += 1
                continue
            c = [f["credorNaFila"] for f in filas]
            n = sum(x["nObrigacoes"] for x in c)
            soma = {k: sum(x[k] for x in c) for k in ("nPagas", "ultrapassagensPagas", "nNaoPagas",
                                                          "ultrapassagensNaoPagas")}  # fmt: skip
            espera = max(x["maiorEsperaNaoPagaDias"] for x in c)
            print(
                f"   credor: {n} obrigações em {len(c)} fila(s); principal: {soma['nPagas']} pagas, "
                f"{soma['ultrapassagensPagas']} ultrapassagens; auxiliar: {soma['nNaoPagas']} não pagas, "
                f"{soma['ultrapassagensNaoPagas']} ultrapassagens, maior espera {espera} dias"
            )
            principal = _posicao(m["rankingPrincipal"], caso.cnpj, m["credoresNoNivelPrincipal"])
            auxiliar = _posicao(m["listaAuxiliarNaoPagas"], caso.cnpj, m["credoresComNaoPagas"])
            print(f"   posição: principal {principal}; auxiliar {auxiliar}")
            if n != caso.obrigacoes:
                print(f"   FALHA: esperadas {caso.obrigacoes} obrigações do credor, vieram {n}")
                falhas += 1
    print(f"\n{len(CASOS) - falhas} de {len(CASOS)} casos conferidos")
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
