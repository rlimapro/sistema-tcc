"""Sondagem T09: filtra arquivos `Socios` da RFB pelas raízes de CNPJ de uma lista.

Uso: python -I rfb_filtra_por_raiz.py raizes.txt saida.csv ARQ [ARQ...]
A raiz é lida da linha crua (`linha[1:9]`), sem parse, como no leitor do ETL 2.
"""

import sys
import time


def main() -> None:
    raizes = {linha.strip().strip('"')[:8].encode() for linha in open(sys.argv[1]) if linha.strip()}
    inicio, lidas, filtradas = time.time(), 0, 0
    with open(sys.argv[2], "wb") as saida:
        for arquivo in sys.argv[3:]:
            with open(arquivo, "rb") as entrada:
                for linha in entrada:
                    lidas += 1
                    if linha[1:9] in raizes:
                        saida.write(linha)
                        filtradas += 1
            print(arquivo, lidas, filtradas, f"{time.time() - inicio:.1f}s", flush=True)
    print("total linhas", lidas, "filtradas", filtradas, f"{time.time() - inicio:.1f}s")


if __name__ == "__main__":
    main()
