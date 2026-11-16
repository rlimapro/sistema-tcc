"""Sondagem T09: compara o QSA de um snapshot antigo com o de 2026-08, nas raízes do antigo.

Uso:
  python -I rfb_compara_snapshots.py rfb  antigo.csv novo.csv AAAAMMDD   # dois CSVs da RFB (";", latin-1)
  python -I rfb_compara_snapshots.py bio  bio.csv    novo.csv AAAAMMDD   # Brasil.IO socios.csv (",", utf-8)

AAAAMMDD é a data do snapshot antigo. Chave do vínculo: (tipo, documento, nome) para PF e (tipo, raiz do
sócio) para PJ, porque o documento PJ tem 14 dígitos em 2023-05 e 8 em 2026-08. Mede saídas, entradas,
datas de entrada alteradas (hipótese H-1) e trocas de qualificação.
"""

import collections
import csv
import sys


def _chave(tipo: str, documento: str, nome: str) -> tuple[str, str, str]:
    return ("1", documento[:8], "") if tipo == "1" else (tipo, documento, nome)


def ler_rfb(caminho: str) -> dict[str, dict]:
    qsa: dict[str, dict] = collections.defaultdict(dict)
    with open(caminho, encoding="latin-1", newline="") as arquivo:
        for r in csv.reader(arquivo, delimiter=";"):
            qsa[r[0]][_chave(r[1], r[3], r[2])] = (r[4], r[5])
    return qsa


def ler_brasilio(caminho: str) -> dict[str, dict]:
    qsa: dict[str, dict] = collections.defaultdict(dict)
    with open(caminho, encoding="utf-8", newline="") as arquivo:
        for r in csv.reader(arquivo):
            qsa[r[0][:8]][_chave(r[5], r[2], r[3])] = (r[4].zfill(2), None)
    return qsa


def comparar(antigo: dict[str, dict], novo: dict[str, dict], corte: str) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for raiz, socios in antigo.items():
        if raiz not in novo:
            c["raiz_sumiu"] += 1
            continue
        atuais = novo[raiz]
        for chave, (qual, _data) in socios.items():
            c["vinculos_antigos"] += 1
            if chave not in atuais:
                c["saiu"] += 1
                if any(k[:2] == chave[:2] for k in atuais) and chave[0] == "2":
                    c["saiu_mas_documento_presente"] += 1
                continue
            c["mantido"] += 1
            qual_novo, data_nova = atuais[chave]
            mudou_qual = qual_novo != qual
            c["mantido_qual_mudou"] += mudou_qual
            if data_nova > corte:
                c["data_nova_posterior_ao_snapshot_antigo"] += 1
                c["  com_qual_mudada" if mudou_qual else "  com_qual_igual"] += 1
        for chave, (_qual, data_nova) in atuais.items():
            if chave not in socios:
                c["novo_entrada_apos_snapshot" if data_nova > corte else "novo_entrada_ate_snapshot"] += 1
    return c


def main() -> None:
    modo, antigo, novo, corte = sys.argv[1:5]
    contagem = comparar(ler_rfb(antigo) if modo == "rfb" else ler_brasilio(antigo), ler_rfb(novo), corte)
    for chave, valor in sorted(contagem.items()):
        print(f"{chave}: {valor}")


if __name__ == "__main__":
    main()
