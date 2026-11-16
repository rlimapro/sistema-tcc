import pytest

from sarc.grafo.esquema import CHAVES, INDICES, aplicar_esquema, listar_esquema

pytestmark = pytest.mark.integration


def test_esquema_idempotente(neo4j_teste) -> None:
    antes = listar_esquema(neo4j_teste)
    aplicar_esquema(neo4j_teste)
    aplicar_esquema(neo4j_teste)
    depois = listar_esquema(neo4j_teste)
    assert antes == depois
    constraints = {(c["name"], c["rotulo"], c["propriedade"]) for c in depois["constraints"]}
    assert constraints == set(CHAVES)
    indices = {(i["name"], i["rotulo"], i["propriedade"]) for i in depois["indices"]}
    assert indices == set(INDICES)
    assert all(i["state"] == "ONLINE" for i in depois["indices"])


def test_unicidade_vale(neo4j_teste) -> None:
    from neo4j.exceptions import ConstraintError

    from sarc.grafo.conexao import escrever

    escrever(neo4j_teste, "CREATE (:Empresa {cnpjBasico: '12345678'})")
    with pytest.raises(ConstraintError):
        escrever(neo4j_teste, "CREATE (:Empresa {cnpjBasico: '12345678'})")
