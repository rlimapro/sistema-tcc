"""Sondagem T09: CNPJs (14 dígitos) dos licitantes PJ no cache do TCE (amostra de fornecedores).

Uso: python -I rfb_cnpjs_do_cache_tce.py <cache tce_sim> saida.txt
"""

import json
import pathlib
import re
import sys


def main() -> None:
    raiz, saida = pathlib.Path(sys.argv[1]), sys.argv[2]
    cnpjs: set[str] = set()
    for arquivo in (raiz / "licitantes_fornecedores_bens_servicos").rglob("*.json"):
        for registro in json.loads(arquivo.read_text()):
            digitos = re.sub(r"\D", "", registro.get("numero_documento_negociante") or "")
            if len(digitos) == 14:
                cnpjs.add(digitos)
    pathlib.Path(saida).write_text("\n".join(sorted(cnpjs)))
    print(len(cnpjs), "CNPJs;", len({c[:8] for c in cnpjs}), "raízes")


if __name__ == "__main__":
    main()
