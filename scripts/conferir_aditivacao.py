"""Confere o nível que a aditivação dá a cada contrato dos casos-base (D-35).

Uso (depois de `scripts/casos_aditivacao.sh` carregar os recortes, ou sobre o grafo da carga completa):
    uv run python scripts/conferir_aditivacao.py --env-file <arquivo>

Imprime, por contrato, o nível e o motivo obtidos, o esperado, as taxas e o limite. Sai com código 1 se
algum divergir. Os esperados foram medidos em 25/10/2026 no cache do v2 (`docs/propostas/aditivacao.md`,
§6). A aditivação não depende da RFB, então o resultado não muda com o snapshot; muda se a fonte trouxer
aditivos novos para esses contratos (o de Quixadá ainda está vigente).
"""

from __future__ import annotations

import argparse
import sys
import time

from sarc.aditivacao.consultas import Escopo, buscar_aditivacao
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver

# contrato original -> (nível, motivo) esperado
ESPERADO: dict[str, tuple[str, str | None]] = {
    "113|2090651|2019-11-11": ("principal", None),  # Morada Nova, TCE 24967/2022-5
    "115|20110143|2011-04-01": ("principal", None),  # Morrinhos, TCE 16382/2018-6
    "005|20130479|2013-08-12": ("auxiliar", "so_na_ampla"),  # Acopiara, TCE 32394/2018-5
    "073|13.02/2019-TP|2019-11-07": ("excluido", "acrescimo_nao_quantificado"),  # Icó, fora do conjunto
    "144|001/2021|2021-04-07": ("principal", None),  # Quixadá, limitação declarada no v2
    "144|002/2021|2021-04-07": ("principal", None),
}


def _pct(valor: float | None) -> str:
    return "-" if valor is None else f"{100 * valor:.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    config = carregar(args.env_file)
    divergentes = 0
    with abrir_driver(config) as driver:
        for contrato, esperado in ESPERADO.items():
            inicio = time.perf_counter()
            avaliacoes = buscar_aditivacao(driver, Escopo.contrato(contrato)).avaliacoes
            a = avaliacoes[0] if avaliacoes else None
            obtido = (a.nivel, a.motivo) if a else ("ausente", None)
            marca = "ok" if obtido == esperado else "DIVERGE"
            divergentes += obtido != esperado
            segundos = time.perf_counter() - inicio
            print(f"{contrato}: {obtido} (esperado {esperado}) [{marca}] {segundos:.2f} s")
            if a is not None:
                print(
                    f"    acréscimo {_pct(a.taxaAcrescimo)}, ampla {_pct(a.taxaAmpla)}, "
                    f"sem reajuste {_pct(a.taxaSemReajuste)}, limite {a.limiteAplicavel}, "
                    f"{len(a.aditivos)} aditivos, fornecedores {[f['cnpj'] for f in a.fornecedores]}"
                )
    print(f"{len(ESPERADO) - divergentes} de {len(ESPERADO)} contratos com o nível esperado")
    sys.exit(1 if divergentes else 0)


if __name__ == "__main__":
    main()
