"""Construtor de grafos pequenos para os testes de conluio e do MCP.

Grava no banco de teste (`neo4j_teste`) os mesmos rótulos, arestas e propriedades que os ETLs
gravam. Cada método é um MERGE curto; os testes montam só o que precisam.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from neo4j import Driver

from sarc.grafo.carga import registrar_carga
from sarc.grafo.conexao import escrever

MUNICIPIO = "087"


class Grafo:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def municipio(self, codigo: str = MUNICIPIO, nome: str = "Itatira") -> Grafo:
        escrever(
            self.driver,
            "MERGE (m:Municipio {codigo: $codigo}) SET m.nome = $nome",
            {"codigo": codigo, "nome": nome},
        )
        return self

    def licitacao(
        self,
        id_: str,
        data: date | None,
        de_fato: bool | None = True,
        colidida: bool | None = False,
        municipio: str = MUNICIPIO,
    ) -> Grafo:
        escrever(
            self.driver,
            "MERGE (l:Licitacao {id: $id}) SET l.municipio = $municipio, l.dataRealizacao = $data, "
            "l.licitacaoDeFato = $deFato, l.chaveColidida = $colidida",
            {"id": id_, "municipio": municipio, "data": data, "deFato": de_fato, "colidida": colidida},
        )
        return self

    def empresa(self, cnpj: str, nome: str | None = None) -> Grafo:
        escrever(
            self.driver,
            "MERGE (e:Estabelecimento {cnpj: $cnpj}) SET e.cnpjBasico = $basico "
            "MERGE (m:Empresa {cnpjBasico: $basico}) SET m.razaoSocial = $nome "
            "MERGE (e)-[:PERTENCE_A]->(m)",
            {"cnpj": cnpj, "basico": cnpj[:8], "nome": nome or f"EMPRESA {cnpj[:8]}"},
        )
        return self

    def participa(self, cnpj: str, licitacao: str, data: date | None) -> Grafo:
        escrever(
            self.driver,
            "MATCH (e:Estabelecimento {cnpj: $cnpj}), (l:Licitacao {id: $lic}) "
            "MERGE (e)-[r:PARTICIPOU_DE]->(l) SET r.dataRealizacao = $data",
            {"cnpj": cnpj, "lic": licitacao, "data": data},
        )
        return self

    def contato_bruto(
        self,
        cnpj: str,
        licitacao: str,
        fones: list[str] | None = None,
        enderecos: list[str] | None = None,
        ceps: list[str] | None = None,
        municipio: str | None = None,
    ) -> Grafo:
        """Contato como a empresa o digitou na 505 (propriedades que o ETL 1 grava em PARTICIPOU_DE)."""
        escrever(
            self.driver,
            "MATCH (:Estabelecimento {cnpj: $cnpj})-[r:PARTICIPOU_DE]->(:Licitacao {id: $lic}) "
            "SET r.foneDeclarado = $fones, r.enderecoDeclarado = $enderecos, r.cepDeclarado = $ceps, "
            "r.municipioDeclarado = $municipio",
            {
                "cnpj": cnpj,
                "lic": licitacao,
                "fones": fones or [],
                "enderecos": enderecos or [],
                "ceps": ceps or [],
                "municipio": municipio,
            },
        )
        return self

    def socio(self, pessoa: str, cnpj_basico: str, data: date | None, qualificacao: str = "49") -> Grafo:
        escrever(
            self.driver,
            "MERGE (p:Pessoa {id: $pessoa}) WITH p MATCH (m:Empresa {cnpjBasico: $basico}) "
            "CREATE (p)-[:SOCIO {dataEntrada: $data, qualificacao: $q, snapshot: 'rfb-2026-08', "
            "fonte: 'rfb'}]->(m)",
            {"pessoa": pessoa, "basico": cnpj_basico, "data": data, "q": qualificacao},
        )
        return self

    def socio_pj(self, socia: str, investida: str, data: date | None, qualificacao: str = "49") -> Grafo:
        escrever(
            self.driver,
            "MATCH (a:Empresa {cnpjBasico: $socia}), (b:Empresa {cnpjBasico: $investida}) "
            "CREATE (a)-[:SOCIO_PJ {dataEntrada: $data, qualificacao: $q, snapshot: 'rfb-2026-08', "
            "fonte: 'rfb'}]->(b)",
            {"socia": socia, "investida": investida, "data": data, "q": qualificacao},
        )
        return self

    def representa(self, pessoa: str, cnpj_basico: str, qualificacao: str = "05") -> Grafo:
        escrever(
            self.driver,
            "MERGE (p:Pessoa {id: $pessoa}) WITH p MATCH (m:Empresa {cnpjBasico: $basico}) "
            "CREATE (p)-[:REPRESENTA {qualificacao: $q, snapshot: 'rfb-2026-08', fonte: 'rfb'}]->(m)",
            {"pessoa": pessoa, "basico": cnpj_basico, "q": qualificacao},
        )
        return self

    def declarou_telefone(self, cnpj: str, numero: str, licitacao: str, data: date) -> Grafo:
        escrever(
            self.driver,
            "MATCH (e:Estabelecimento {cnpj: $cnpj}) MERGE (t:Telefone {numero: $numero}) "
            "CREATE (e)-[:DECLAROU_CONTATO {fonte: 'tce_505', data: $data, licitacao: $lic}]->(t)",
            {"cnpj": cnpj, "numero": numero, "lic": licitacao, "data": data},
        )
        return self

    def declarou_endereco(
        self, cnpj: str, id_: str, licitacao: str, data: date, grau_nacional: int | None = None
    ) -> Grafo:
        escrever(
            self.driver,
            "MATCH (e:Estabelecimento {cnpj: $cnpj}) MERGE (x:Endereco {id: $id}) "
            "SET x.grauNacional = $grau "
            "CREATE (e)-[:DECLAROU_CONTATO {fonte: 'tce_505', data: $data, licitacao: $lic}]->(x)",
            {"cnpj": cnpj, "id": id_, "lic": licitacao, "data": data, "grau": grau_nacional},
        )
        return self

    def telefone_rfb(self, cnpj: str, numero: str, grau_nacional: int | None = None) -> Grafo:
        escrever(
            self.driver,
            "MATCH (e:Estabelecimento {cnpj: $cnpj}) MERGE (t:Telefone {numero: $numero}) "
            "SET t.grauNacional = $grau MERGE (e)-[:TEM_TELEFONE {fonte: 'rfb'}]->(t)",
            {"cnpj": cnpj, "numero": numero, "grau": grau_nacional},
        )
        return self

    def endereco_rfb(self, cnpj: str, id_: str, sem_numero: bool = False) -> Grafo:
        escrever(
            self.driver,
            "MATCH (e:Estabelecimento {cnpj: $cnpj}) MERGE (x:Endereco {id: $id}) "
            "SET x.semNumero = $sn MERGE (e)-[:LOCALIZADO_EM {fonte: 'rfb'}]->(x)",
            {"cnpj": cnpj, "id": id_, "sn": sem_numero},
        )
        return self

    def carga(self, etapa: str, parametros: dict, status: str = "concluida") -> Grafo:
        momento = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        registrar_carga(self.driver, etapa, parametros, {}, momento, momento, status)
        return self


# CNPJs fictícios usados nos testes
A = "11111111000101"
B = "22222222000102"
C = "33333333000103"
D = "44444444000104"


def itatira(g: Grafo, antecipada: bool = False) -> Grafo:
    """Cadeia de Itatira (CRITICA §2.2), reduzida: três empresas e duas pessoas.

    A e B disputam L1 (2019-04-22) e se ligam por 2 saltos (A←P1→B), com a entrada de P1 em B em
    2019-04-24, dois dias depois do certame. A e C disputam L2 (2021-08-10) e se ligam por 4 saltos
    (A←P1→B←P2→C), com a entrada de P2 em C em 2025-07-30. Qualificações 49/49/22/49, como no caso
    real. Na variante `antecipada`, as duas entradas tardias passam para antes dos certames.
    """
    entrada_p1_b = date(2019, 1, 10) if antecipada else date(2019, 4, 24)
    entrada_p2_c = date(2020, 2, 1) if antecipada else date(2025, 7, 30)
    (
        g.municipio()
        .licitacao("087|L1", date(2019, 4, 22))
        .licitacao("087|L2", date(2021, 8, 10))
        .empresa(A)
        .empresa(B)
        .empresa(C)
        .participa(A, "087|L1", date(2019, 4, 22))
        .participa(B, "087|L1", date(2019, 4, 22))
        .participa(A, "087|L2", date(2021, 8, 10))
        .participa(C, "087|L2", date(2021, 8, 10))
        .socio("p1", A[:8], date(2015, 9, 9), "49")
        .socio("p1", B[:8], entrada_p1_b, "49")
        .socio("p2", B[:8], date(2016, 1, 1), "22")
        .socio("p2", C[:8], entrada_p2_c, "49")
    )
    return g


def par_simples(g: Grafo, licitacao: str = "087|P", data: date = date(2020, 6, 1), **lic: object) -> Grafo:
    """A e B participando da mesma licitação, sem vínculo nenhum ainda."""
    g.municipio().licitacao(licitacao, data, **lic).empresa(A).empresa(B)  # type: ignore[arg-type]
    g.participa(A, licitacao, data).participa(B, licitacao, data)
    return g
