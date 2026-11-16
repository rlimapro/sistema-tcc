"""Escolha e extração do snapshot da RFB (D-40)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sarc.etl_rfb.snapshot import NECESSARIOS, SnapshotRFBInvalido, extrair_zips, resolver_snapshot


def _extraido(pasta: Path) -> Path:
    for nome in ("Empresas0", "Estabelecimentos0", "Socios0"):
        (pasta / nome).mkdir(parents=True)
        (pasta / nome / "ARQ.CSV").write_text("x", encoding="latin-1")
    return pasta


def _zips(pasta: Path, nomes: tuple[str, ...] = NECESSARIOS) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in nomes:
        with zipfile.ZipFile(pasta / f"{nome}.zip", "w") as zf:
            zf.writestr(f"K3241.{nome.upper()}CSV", f'"{nome}";"linha"\n')
    return pasta


def test_sem_pedido_usa_o_snapshot_mais_recente(tmp_path: Path) -> None:
    _extraido(tmp_path / "2026-08")
    _extraido(tmp_path / "2026-10")
    (tmp_path / "rascunho").mkdir()
    pasta, rotulo = resolver_snapshot(tmp_path)
    assert (pasta, rotulo) == (tmp_path / "2026-10", "2026-10")


def test_snapshot_pedido_tem_precedencia(tmp_path: Path) -> None:
    _extraido(tmp_path / "2026-08")
    _extraido(tmp_path / "2026-10")
    assert resolver_snapshot(tmp_path, "2026-08") == (tmp_path / "2026-08", "2026-08")
    assert resolver_snapshot(tmp_path, "") == (tmp_path / "2026-10", "2026-10")


def test_caminho_direto_de_um_snapshot_continua_valendo(tmp_path: Path) -> None:
    pasta = _extraido(tmp_path / "2026-08")
    assert resolver_snapshot(pasta) == (pasta, "2026-08")
    assert resolver_snapshot(pasta, "2026-08") == (pasta, "2026-08")


def test_mais_recente_so_com_zips_falha_em_vez_de_cair_no_anterior(tmp_path: Path) -> None:
    _extraido(tmp_path / "2026-08")
    _zips(tmp_path / "2026-10")
    with pytest.raises(SnapshotRFBInvalido, match="só tem os ZIPs"):
        resolver_snapshot(tmp_path)
    assert resolver_snapshot(tmp_path, exigir_extraido=False) == (tmp_path / "2026-10", "2026-10")


def test_formato_invalido_e_pasta_vazia(tmp_path: Path) -> None:
    with pytest.raises(SnapshotRFBInvalido, match="AAAA-MM"):
        resolver_snapshot(tmp_path, "outubro")
    with pytest.raises(SnapshotRFBInvalido, match="nenhuma pasta"):
        resolver_snapshot(tmp_path)


def test_extrair_zips_cria_uma_pasta_por_particao_e_e_idempotente(tmp_path: Path) -> None:
    pasta = _zips(tmp_path / "2026-10", NECESSARIOS + ("Simples",))
    assert extrair_zips(pasta) == {"extraidos": len(NECESSARIOS), "ja_extraidos": 0}
    assert (pasta / "Socios0" / "K3241.SOCIOS0CSV").read_text() == '"Socios0";"linha"\n'
    assert not (pasta / "Simples").exists()  # o que o ETL não lê não é extraído
    assert not list(pasta.glob("*.tmp"))
    assert extrair_zips(pasta) == {"extraidos": 0, "ja_extraidos": len(NECESSARIOS)}
    assert resolver_snapshot(tmp_path) == (pasta, "2026-10")


def test_extrair_recusa_zip_com_caminho(tmp_path: Path) -> None:
    pasta = _zips(tmp_path / "2026-10", NECESSARIOS[1:])
    with zipfile.ZipFile(pasta / f"{NECESSARIOS[0]}.zip", "w") as zf:
        zf.writestr("../fora.csv", "x")
    with pytest.raises(SnapshotRFBInvalido, match="nome de arquivo inesperado"):
        extrair_zips(pasta)
    assert not (tmp_path / "fora.csv").exists()
