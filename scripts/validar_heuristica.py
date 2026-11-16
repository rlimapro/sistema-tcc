"""Mede a heurística liquidação → pagamento contra a ligação oficial, por município (D-36).

Uso:
    uv run python scripts/validar_heuristica.py --env-file <arquivo> --municipio 100,104,087 \
        [--exercicio 2025 --restos 3] [--cache-extra data/raw/tce_sim_v2]

Só lê o cache (nada vai para o grafo). Para cada município, compara obrigação a obrigação, entre as
liquidadas em 2025 (a tabela de ligação só existe em remessas de 2025 em diante), a data da heurística
sozinha com a da ligação sozinha, e separa o erro por tipo: data diferente; falso não pago (a heurística
não acha o pagamento que a ligação tem: infla o componente auxiliar); falso pago (o contrário, só com
pagamento até 2025-12-31, porque a ligação de 2026 é incompleta na fonte em vários municípios).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path

from sarc.config import carregar
from sarc.pretericao import leitor
from sarc.pretericao.calculo import (
    ACERTO,
    AMBOS_NAO_PAGO,
    DATA_DIFERENTE,
    FALSO_NAO_PAGO,
    FALSO_PAGO,
    SEM_VERDADE,
    comparar_heuristica,
)

TIPOS = (ACERTO, DATA_DIFERENTE, FALSO_NAO_PAGO, FALSO_PAGO, AMBOS_NAO_PAGO, SEM_VERDADE)


def _pct(parte: int, todo: int) -> str:
    return f"{100 * parte / todo:.1f}%" if todo else "-"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--municipio", required=True, help="códigos separados por vírgula")
    parser.add_argument("--exercicio", type=int, default=date.today().year - 1)
    parser.add_argument("--restos", type=int, default=3)
    parser.add_argument("--cache-extra", action="append", type=Path, default=[])
    args = parser.parse_args()
    config = carregar(args.env_file)
    extrator = leitor.extrator_so_cache(config.tce_cache_path, args.cache_extra)
    janela = leitor.Janela(args.exercicio, args.restos)
    total: Counter[str] = Counter()
    print(
        "município | n | acerto | data diferente | falso não pago | falso pago | ambos não pago | sem verdade"
    )
    for municipio in [m.strip().zfill(3) for m in args.municipio.split(",") if m.strip()]:
        lido = leitor.ler_municipio(municipio, janela, extrator)
        if not lido.completa:
            print(f"{municipio} | NÃO medido: {len(lido.faltando)} baldes faltando")
            continue
        r = comparar_heuristica(lido.empenhos, lido.liquidacoes, lido.pagamentos, lido.ligacoes)
        total.update(r)
        n = sum(r.values())
        print(f"{municipio} | {n} | " + " | ".join(f"{r[t]} ({_pct(r[t], n)})" for t in TIPOS))
        pagas_h = r[ACERTO] + r[DATA_DIFERENTE] + r[FALSO_PAGO]
        nao_pagas_h = r[FALSO_NAO_PAGO] + r[AMBOS_NAO_PAGO]
        print(
            f"    das que a heurística deixa sem pagamento, a ligação paga "
            f"{_pct(r[FALSO_NAO_PAGO], nao_pagas_h)}; "
            f"das que ela paga, a data bate em {_pct(r[ACERTO], pagas_h)}"
        )
    n = sum(total.values())
    print(f"total | {n} | " + " | ".join(f"{total[t]} ({_pct(total[t], n)})" for t in TIPOS))


if __name__ == "__main__":
    main()
