"""Distribuição dos contratos de aditivação por ano do original e por município, em JSON (D-35).

Uso:
    uv run python scripts/distribuicao_aditivacao.py --env-file .env \
        [--municipio 087] [--desde 2009] [--ate 2026]

Sem `--municipio`, percorre todos os municípios do grafo, um de cada vez (timeout por município).
Cada linha traz nível, motivo, número de contratos e o excesso em reais somado só dentro da célula.
Os contratos `dentro` e excluídos entram como denominador; as contagens (inclusive órfãos) vêm somadas.
Com --desde/--ate, os contratos são filtrados pela data do ORIGINAL e os aditivos órfãos pela data do
próprio ADITIVO: não divida um pelo outro, nem compare órfãos entre recortes de janelas diferentes.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from sarc.aditivacao.consultas import distribuicao_aditivacao
from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--municipio", default=None, help="código do TCE-CE (ex.: 087)")
    parser.add_argument("--desde", type=int, default=None, help="ano inicial do contrato original")
    parser.add_argument("--ate", type=int, default=None, help="ano final do contrato original")
    parser.add_argument("--timeout", type=float, default=120.0, help="por município, em segundos")
    args = parser.parse_args()
    desde = date(args.desde, 1, 1) if args.desde else None
    ate = date(args.ate, 12, 31) if args.ate else None
    municipio = args.municipio.zfill(3) if args.municipio else None
    with abrir_driver(carregar(args.env_file)) as driver:
        saida = distribuicao_aditivacao(driver, municipio, desde, ate, args.timeout)
    print(json.dumps(saida, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
