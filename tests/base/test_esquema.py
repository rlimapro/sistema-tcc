from sarc.grafo.esquema import CHAVES, INDICES, comandos


def test_comandos_idempotentes_e_completos() -> None:
    cmds = comandos()
    assert len(cmds) == len(CHAVES) + len(INDICES)
    assert all("IF NOT EXISTS" in c for c in cmds)
    rotulos = {rotulo for _, rotulo, _ in CHAVES}
    assert rotulos == {
        "Municipio",
        "Orgao",
        "Licitacao",
        "Contrato",
        "Empresa",
        "Estabelecimento",
        "Pessoa",
        "Telefone",
        "Email",
        "Endereco",
        "Carga",
        "FilaPagamento",
        "ResumoPretericao",
    }
