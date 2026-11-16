"""Confere em que lista a tool `conluio_na_licitacao` põe cada licitação dos casos-base (D-32).

Uso (depois de `scripts/casos_reais.sh` carregar os recortes):
    uv run python scripts/conferir_licitacoes.py --env-file <arquivo>

Imprime, por licitação, o nível obtido (principal, auxiliar, posterior ou ausente), o esperado e os
vínculos. Sai com código 1 se algum nível divergir do esperado. Os esperados foram medidos em 25/10/2026
com o recorte de `casos_reais.sh` e o grau nacional da RFB 2026-08; numa carga maior o resultado pode
mudar (novas cadeias societárias, colisão de chave), por isso a conferência se repete depois da carga.
"""

from __future__ import annotations

import argparse
import sys
import time

from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver
from sarc.mcp_server.ferramentas import conluio_na_licitacao

# licitação -> nível esperado (D-32)
ESPERADO: dict[str, str] = {
    "170|200901202": "auxiliar",
    "170|200901133": "auxiliar",
    "170|201012171": "auxiliar",
    "098|0809012022": "principal",
    "098|0310012022": "auxiliar",
    "087|2901012021CP": "principal",
    "087|1308022021PE": "principal",
}

LISTAS = (
    ("alertas", "principal"),
    ("alertasConfiancaDesconhecida", "auxiliar"),
    ("vinculosPosterioresAoCertame", "posterior"),
)


def nivel(dados: dict) -> str:
    for lista, nome in LISTAS:
        if dados.get(lista):
            return nome
    return "ausente"


def resumo_vinculos(dados: dict) -> list[str]:
    linhas = []
    for lista, nome in LISTAS:
        for ocorrencia in dados.get(lista) or []:
            vinculos = ocorrencia.get("vinculos") or []
            tipos = sorted({f"{v.get('tipoVinculo')}/{v.get('fonte')}" for v in vinculos})
            linhas.append(f"    {nome}: {', '.join(tipos) or '-'}")
    return linhas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    config = carregar(args.env_file)
    divergentes = 0
    with abrir_driver(config) as driver:
        for licitacao, esperado in ESPERADO.items():
            inicio = time.perf_counter()
            dados = conluio_na_licitacao(driver, licitacao, config.grau_maximo_contato)["dados"]
            obtido = nivel(dados)
            marca = "ok" if obtido == esperado else "DIVERGE"
            divergentes += obtido != esperado
            print(
                f"{licitacao}: {obtido} (esperado {esperado}) [{marca}] {time.perf_counter() - inicio:.2f} s"
            )
            for linha in resumo_vinculos(dados):
                print(linha)
    print(f"{len(ESPERADO) - divergentes} de {len(ESPERADO)} licitações com o nível esperado")
    sys.exit(1 if divergentes else 0)


if __name__ == "__main__":
    main()
