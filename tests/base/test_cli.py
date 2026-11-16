from click.testing import CliRunner

from sarc.cli import SUBCOMANDOS, grupo


def test_help_lista_os_subcomandos() -> None:
    resultado = CliRunner().invoke(grupo, ["--help"])
    assert resultado.exit_code == 0, resultado.output
    for nome in ["esquema", *SUBCOMANDOS]:
        assert nome in resultado.output


def test_env_file_inexistente_e_erro_de_uso(tmp_path) -> None:
    resultado = CliRunner().invoke(grupo, ["--env-file", str(tmp_path / "x.env"), "esquema"])
    assert resultado.exit_code == 2
    assert "não encontrado" in resultado.output


def test_stub_de_modulo_ausente(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(SUBCOMANDOS, "fantasma", "sarc.modulo_que_nao_existe.cli")
    vazio = tmp_path / "vazio.env"
    vazio.write_text("", encoding="utf-8")
    resultado = CliRunner().invoke(grupo, ["--env-file", str(vazio), "fantasma"])
    assert resultado.exit_code == 2
    assert "ainda não implementado" in resultado.output
