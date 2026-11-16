import logging
import os
from pathlib import Path

import pytest

from sarc.config import Config, ErroDeConfiguracao, carregar, ler_env_file


@pytest.fixture(autouse=True)
def _ambiente_limpo(monkeypatch: pytest.MonkeyPatch) -> None:
    # O conftest restaura os.environ inteiro depois de cada teste.
    for nome in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "TCE_PAGE_SIZE", "LOG_FILE"):
        monkeypatch.delenv(nome, raising=False)


def test_env_file_sobrescreve_o_ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("NEO4J_URI", "bolt://banco-errado:7687")
    arquivo = tmp_path / "alvo.env"
    arquivo.write_text("NEO4J_URI=bolt://localhost:7699\nNEO4J_PASSWORD=segredo123\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="sarc.config"):
        config = carregar(arquivo)
    assert config.neo4j_uri == "bolt://localhost:7699"
    assert config.env_file == arquivo.resolve()
    assert "bolt://localhost:7699" in caplog.text
    assert "segredo123" not in caplog.text
    assert "segredo123" not in repr(config)


def test_senha_obrigatoria(tmp_path: Path) -> None:
    arquivo = tmp_path / "sem_senha.env"
    arquivo.write_text("# obrigatório\nNEO4J_PASSWORD=\n", encoding="utf-8")
    with pytest.raises(ErroDeConfiguracao, match="NEO4J_PASSWORD"):
        carregar(arquivo)


def test_arquivo_explicito_inexistente_e_erro(tmp_path: Path) -> None:
    with pytest.raises(ErroDeConfiguracao, match="não encontrado"):
        ler_env_file(tmp_path / "nao_existe.env")


def test_defaults_e_tipos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "x" * 8)
    config = Config.do_ambiente()
    assert config.neo4j_uri == "bolt://localhost:7699"
    assert config.tce_timeout == (10.0, 60.0)
    assert config.tce_page_size == 1000
    assert config.log_file is None


def test_inteiro_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "x" * 8)
    monkeypatch.setenv("TCE_PAGE_SIZE", "mil")
    with pytest.raises(ErroDeConfiguracao, match="TCE_PAGE_SIZE"):
        Config.do_ambiente()


def test_env_example_lido_sem_erro_de_sintaxe(monkeypatch: pytest.MonkeyPatch) -> None:
    exemplo = Path(__file__).resolve().parents[2] / ".env.example"
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    ler_env_file(exemplo)
    assert os.environ["NEO4J_URI"] == "bolt://localhost:7699"
    assert os.environ["TCE_PAGE_SIZE"] == "1000"
    assert os.environ["NEO4J_PASSWORD"] == ""
    assert os.environ["LOG_FILE"] == ""


def test_senha_curta_recusada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "curta")
    with pytest.raises(ErroDeConfiguracao, match="8 caracteres"):
        Config.do_ambiente()
