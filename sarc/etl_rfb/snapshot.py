"""Escolha e extração do snapshot da RFB (D-40).

A RFB publica um mês por vez (`AAAA-MM`). O v3 não fixa o mês: `RFB_DATA_PATH` aponta para a pasta que
guarda os snapshots (padrão `./data/rfb`, com subpastas `2026-08/`, `2026-10/`...) e o snapshot em uso é
o pedido em `--snapshot`/`RFB_SNAPSHOT` ou, sem pedido, a subpasta `AAAA-MM` **mais recente**. Por
compatibilidade, `RFB_DATA_PATH` também pode apontar direto para a pasta de um snapshot (a que contém
`Socios0/`); nesse caso o nome dela é o snapshot.

O ETL 2 lê CSVs extraídos, um por pasta (`Socios0/<arquivo>`). Se o snapshot só tiver os ZIPs baixados
do portal, `extrair_zips` os extrai (só os tipos que o ETL usa), um ZIP por pasta, com gravação atômica.
"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

PADRAO_SNAPSHOT = re.compile(r"^\d{4}-\d{2}$")

# Partições que o ETL 2 e o grau nacional leem; o resto do portal (Simples, Cnaes...) não é extraído.
NECESSARIOS = tuple(
    [f"{tipo}{i}" for tipo in ("Empresas", "Estabelecimentos", "Socios") for i in range(10)] + ["Municipios"]
)


class SnapshotRFBInvalido(RuntimeError):
    """O snapshot pedido (ou o mais recente) não está pronto para o ETL 2."""


def _extraido(pasta: Path) -> bool:
    return all((pasta / nome).is_dir() for nome in ("Empresas0", "Estabelecimentos0", "Socios0"))


def _tem_zips(pasta: Path) -> bool:
    return (pasta / "Socios0.zip").is_file()


def resolver_snapshot(
    raiz: Path, snapshot: str | None = None, *, exigir_extraido: bool = True
) -> tuple[Path, str]:
    """`(pasta do snapshot, rótulo AAAA-MM)` a partir de `RFB_DATA_PATH` e do snapshot pedido (ou None)."""
    raiz = Path(raiz)
    snapshot = (snapshot or "").strip() or None
    if snapshot is not None and not PADRAO_SNAPSHOT.match(snapshot):
        raise SnapshotRFBInvalido(f"snapshot da RFB deve ter o formato AAAA-MM, veio {snapshot!r}")

    if _extraido(raiz) or _tem_zips(raiz):  # RFB_DATA_PATH já é a pasta de um snapshot
        pasta, rotulo = raiz, snapshot or raiz.name
        if not PADRAO_SNAPSHOT.match(rotulo):
            raise SnapshotRFBInvalido(
                f"{raiz} parece um snapshot, mas o nome não é AAAA-MM; indique o mês com --snapshot"
            )
    elif snapshot is not None:
        pasta, rotulo = raiz / snapshot, snapshot
    else:
        candidatos = (
            sorted(p for p in raiz.iterdir() if p.is_dir() and PADRAO_SNAPSHOT.match(p.name))
            if raiz.is_dir()
            else []
        )
        if not candidatos:
            raise SnapshotRFBInvalido(f"nenhuma pasta AAAA-MM com dados da RFB em {raiz}")
        pasta, rotulo = candidatos[-1], candidatos[-1].name

    if exigir_extraido and not _extraido(pasta):
        if _tem_zips(pasta):
            raise SnapshotRFBInvalido(
                f"o snapshot {rotulo} ({pasta}) só tem os ZIPs; extraia com `sarc etl-rfb --extrair` "
                "(ou passe --snapshot para usar outro mês)"
            )
        raise SnapshotRFBInvalido(f"snapshot {rotulo} sem os CSVs da RFB em {pasta}")
    return pasta, rotulo


def extrair_zips(pasta: Path) -> dict[str, int]:
    """Extrai `<Tipo><n>.zip` em `<Tipo><n>/<arquivo>`, só para as partições de `NECESSARIOS`.

    Idempotente: pasta já extraída é pulada. Cada ZIP do portal tem um único arquivo; nomes com caminho
    (`/`, `..`) são recusados. A extração vai para um `.tmp` e só então vira a pasta definitiva, então uma
    interrupção nunca deixa uma partição pela metade.
    """
    contagem = {"extraidos": 0, "ja_extraidos": 0}
    for nome in NECESSARIOS:
        destino = pasta / nome
        if destino.is_dir():
            contagem["ja_extraidos"] += 1
            continue
        arquivo_zip = pasta / f"{nome}.zip"
        if not arquivo_zip.is_file():
            raise SnapshotRFBInvalido(f"falta {arquivo_zip} (nem a pasta {destino} existe)")
        temporario = pasta / f"{nome}.tmp"
        if temporario.exists():
            shutil.rmtree(temporario)
        temporario.mkdir()
        with zipfile.ZipFile(arquivo_zip) as zf:
            membros = [m for m in zf.infolist() if not m.is_dir()]
            if len(membros) != 1:
                raise SnapshotRFBInvalido(f"{arquivo_zip}: esperado 1 arquivo, há {len(membros)}")
            membro = membros[0]
            if "/" in membro.filename or "\\" in membro.filename or membro.filename in ("", ".", ".."):
                raise SnapshotRFBInvalido(f"{arquivo_zip}: nome de arquivo inesperado {membro.filename!r}")
            logger.info("extraindo %s (%.1f GB)", arquivo_zip.name, membro.file_size / 1e9)
            with zf.open(membro) as origem, open(temporario / membro.filename, "wb") as saida:
                shutil.copyfileobj(origem, saida, length=16 * 1024 * 1024)
        temporario.rename(destino)
        contagem["extraidos"] += 1
    return contagem
