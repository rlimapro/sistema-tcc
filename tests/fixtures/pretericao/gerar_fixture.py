"""Gera a fixture real reduzida da preterição a partir do cache do v2 (só leitura).

Recorte: Iguatu (074), exercício 2017, órgão 06 (Saúde), fonte 2/03, categoria `bens` — a fila dos seis
empenhos do acórdão da HF Pneus (processo TCE 25037/2019-8). Mantém todos os meses de remessa esperados
(arquivo `[]` quando não há registro), só os registros dessa fila e só os campos que o módulo usa ou que a
validação do balde confere; nomes e CPFs cifrados de responsáveis são descartados.

Uso: uv run python tests/fixtures/pretericao/gerar_fixture.py data/raw/tce_sim_v2
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MUNICIPIO, EXERCICIO, ORGAO, FONTE, ELEMENTOS_BENS = "074", 2017, "06", ("2", "03"), {"30", "52"}
DESTINO = Path(__file__).parent / "cache"

CAMPOS = {
    "notas_empenhos": [
        "codigo_municipio", "exercicio_orcamento", "data_referencia_doc", "codigo_orgao", "numero_empenho",
        "codigo_elemento_despesa", "cd_subelem_desp", "tipo_fonte", "codigo_fonte",
        "numero_documento_negociante", "codigo_tipo_negociante", "data_emissao_empenho",
    ],
    "liquidacoes": [
        "codigo_municipio", "exercicio_orcamento", "data_referencia_doc", "codigo_orgao", "numero_empenho",
        "numero_sub_empenho_liquidacao", "data_liquidacao", "valor_liquidado", "status_estornado",
    ],
    "notas_pagamentos": [
        "codigo_municipio", "exercicio_orcamento", "data_referencia_doc", "codigo_orgao", "numero_empenho",
        "numero_sub_empenho", "numero_nota_pagamento", "data_nota_pagamento", "valor_nota_pagamento",
        "estado_de_estornado",
    ],
}  # fmt: skip
MESES = {"notas_empenhos": 12, "liquidacoes": 24, "notas_pagamentos": 24}


def main(origem: Path) -> None:
    def ler(endpoint: str, mes: str) -> list[dict]:
        pasta = origem / endpoint / MUNICIPIO
        nome = f"{EXERCICIO}00_{mes}"
        rodadas = sorted(pasta.glob(f"{nome}_r*.json")) or [pasta / f"{nome}.json"]
        unidos: dict[str, dict] = {}  # une as rodadas do v2 sem repetir registro
        for caminho in rodadas:
            for r in json.loads(caminho.read_bytes()):
                unidos.setdefault(json.dumps(r, sort_keys=True), r)
        return list(unidos.values())

    def meses(endpoint: str) -> list[str]:
        return [f"{EXERCICIO + i // 12}{i % 12 + 1:02d}" for i in range(MESES[endpoint])]

    da_fila: set[str] = set()
    for mes in meses("notas_empenhos"):
        for r in ler("notas_empenhos", mes):
            fonte = (str(r["tipo_fonte"]).strip(), str(r["codigo_fonte"]).strip())
            if (
                r["codigo_orgao"] == ORGAO
                and fonte == FONTE
                and r["codigo_elemento_despesa"][4:6] in ELEMENTOS_BENS
            ):
                da_fila.add(r["numero_empenho"])
    for endpoint, campos in CAMPOS.items():
        pasta = DESTINO / endpoint / MUNICIPIO
        pasta.mkdir(parents=True, exist_ok=True)
        for mes in meses(endpoint):
            registros = [
                {c: r.get(c) for c in campos}
                for r in ler(endpoint, mes)
                if r["codigo_orgao"] == ORGAO and r["numero_empenho"] in da_fila
            ]
            (pasta / f"{EXERCICIO}00_{mes}.json").write_text(
                json.dumps(registros, ensure_ascii=False, indent=0)
            )
    print(f"{len(da_fila)} empenhos na fila; fixture em {DESTINO}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
