"""As quatro consultas do benchmark, em Cypher e em SQL equivalente (CRITICA §2.1).

Modelo do grafo (ETL 1 e ETL 2 do v3):
    (Estabelecimento {cnpj})-[:PARTICIPOU_DE]->(Licitacao {id, dataRealizacao})
    (Estabelecimento)-[:PERTENCE_A]->(Empresa {cnpjBasico})
    (Pessoa {id})-[:SOCIO {dataEntrada}]->(Empresa)
    (Empresa)-[:SOCIO_PJ {dataEntrada}]->(Empresa)
    (Estabelecimento)-[:TEM_TELEFONE|TEM_EMAIL|LOCALIZADO_EM]->(Telefone|Email|Endereco)

Modelo relacional (benchmark/dados.py): uma tabela por nó e por relação, e
`estabelecimento.cnpj_basico` no lugar de PERTENCE_A (chave estrangeira, como um
projetista relacional faria).

Regras que valem nos dois lados, para que o resultado seja o mesmo:
- par = dois estabelecimentos de raízes de CNPJ diferentes (filiais da mesma empresa não contam), com
  `cnpj1 < cnpj2`;
- vínculo societário só conta se `dataEntrada <= dataRealizacao` do certame (data nula não conta);
- saída sem repetição (DISTINCT), conferida por contagem e hash das linhas ordenadas.

Estas consultas medem o custo de travessia; não substituem a consulta de conluio de `sarc/conluio/`,
que tem mais regras (qualificação, nível, confiança temporal).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Consulta:
    nome: str
    saltos: str
    descricao: str
    cypher: str
    sql: str
    parametrizada: bool = False  # recebe $id / %(id)s (a licitação de referência)


Q0 = Consulta(
    nome="Q0",
    saltos="0",
    descricao="Custo fixo de uma ida e volta ao banco, sem dados (RETURN 1 / SELECT 1), para ler as demais",
    cypher="RETURN 1 AS um",
    sql="SELECT 1 AS um",
)

Q1 = Consulta(
    nome="Q1",
    saltos="1",
    descricao="Participantes de uma licitação (a de mais participantes na amostra)",
    cypher="""
MATCH (e:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao {id: $id})
RETURN DISTINCT e.cnpj AS cnpj
""",
    sql="""
SELECT DISTINCT p.cnpj AS cnpj
FROM participou_de p
WHERE p.licitacao_id = %(id)s
""",
    parametrizada=True,
)

Q2 = Consulta(
    nome="Q2",
    saltos="2 (empresa-sócio-empresa); 6 de licitação a licitação",
    descricao="Pares de coparticipantes com sócio PF em comum, com o filtro de data de entrada",
    cypher="""
MATCH (e1:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao)<-[:PARTICIPOU_DE]-(e2:Estabelecimento)
WHERE e1.cnpj < e2.cnpj AND left(e1.cnpj, 8) <> left(e2.cnpj, 8)
MATCH (e1)-[:PERTENCE_A]->(:Empresa)<-[s1:SOCIO]-(:Pessoa)-[s2:SOCIO]->(:Empresa)<-[:PERTENCE_A]-(e2)
WHERE s1.dataEntrada <= l.dataRealizacao AND s2.dataEntrada <= l.dataRealizacao
RETURN DISTINCT l.id AS id, e1.cnpj AS cnpj1, e2.cnpj AS cnpj2
""",
    sql="""
SELECT DISTINCT l.id AS id, e1.cnpj AS cnpj1, e2.cnpj AS cnpj2
FROM licitacao l
JOIN participou_de p1 ON p1.licitacao_id = l.id
JOIN participou_de p2 ON p2.licitacao_id = l.id
JOIN estabelecimento e1 ON e1.cnpj = p1.cnpj
JOIN estabelecimento e2 ON e2.cnpj = p2.cnpj
JOIN socio s1 ON s1.cnpj_basico = e1.cnpj_basico
JOIN socio s2 ON s2.pessoa_id = s1.pessoa_id AND s2.cnpj_basico = e2.cnpj_basico
WHERE e1.cnpj < e2.cnpj AND left(e1.cnpj, 8) <> left(e2.cnpj, 8)
  AND s1.data_entrada <= l.data_realizacao AND s2.data_entrada <= l.data_realizacao
""",
)

Q3 = Consulta(
    nome="Q3",
    saltos="2 a 4 (sócio, telefone, e-mail ou endereço)",
    descricao="Pares de coparticipantes ligados por sócio PF (com data), telefone, e-mail ou endereço",
    cypher="""
MATCH (e1:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao)<-[:PARTICIPOU_DE]-(e2:Estabelecimento)
WHERE e1.cnpj < e2.cnpj AND left(e1.cnpj, 8) <> left(e2.cnpj, 8)
CALL (e1, e2, l) {
  MATCH (e1)-[:PERTENCE_A]->(:Empresa)<-[s1:SOCIO]-(:Pessoa)-[s2:SOCIO]->(:Empresa)<-[:PERTENCE_A]-(e2)
  WHERE s1.dataEntrada <= l.dataRealizacao AND s2.dataEntrada <= l.dataRealizacao
  RETURN 'socio' AS tipo
  UNION
  MATCH (e1)-[:TEM_TELEFONE]->(:Telefone)<-[:TEM_TELEFONE]-(e2)
  RETURN 'telefone' AS tipo
  UNION
  MATCH (e1)-[:TEM_EMAIL]->(:Email)<-[:TEM_EMAIL]-(e2)
  RETURN 'email' AS tipo
  UNION
  MATCH (e1)-[:LOCALIZADO_EM]->(:Endereco)<-[:LOCALIZADO_EM]-(e2)
  RETURN 'endereco' AS tipo
}
RETURN DISTINCT l.id AS id, e1.cnpj AS cnpj1, e2.cnpj AS cnpj2, tipo
""",
    sql="""
WITH pares AS (
  SELECT DISTINCT l.id, l.data_realizacao, p1.cnpj AS cnpj1, p2.cnpj AS cnpj2
  FROM licitacao l
  JOIN participou_de p1 ON p1.licitacao_id = l.id
  JOIN participou_de p2 ON p2.licitacao_id = l.id
  WHERE p1.cnpj < p2.cnpj AND left(p1.cnpj, 8) <> left(p2.cnpj, 8)
)
SELECT pr.id, pr.cnpj1, pr.cnpj2, 'socio' AS tipo
FROM pares pr
JOIN estabelecimento e1 ON e1.cnpj = pr.cnpj1
JOIN estabelecimento e2 ON e2.cnpj = pr.cnpj2
JOIN socio s1 ON s1.cnpj_basico = e1.cnpj_basico
JOIN socio s2 ON s2.pessoa_id = s1.pessoa_id AND s2.cnpj_basico = e2.cnpj_basico
WHERE s1.data_entrada <= pr.data_realizacao AND s2.data_entrada <= pr.data_realizacao
UNION
SELECT pr.id, pr.cnpj1, pr.cnpj2, 'telefone'
FROM pares pr
JOIN tem_telefone t1 ON t1.cnpj = pr.cnpj1
JOIN tem_telefone t2 ON t2.cnpj = pr.cnpj2 AND t2.numero = t1.numero
UNION
SELECT pr.id, pr.cnpj1, pr.cnpj2, 'email'
FROM pares pr
JOIN tem_email m1 ON m1.cnpj = pr.cnpj1
JOIN tem_email m2 ON m2.cnpj = pr.cnpj2 AND m2.endereco = m1.endereco
UNION
SELECT pr.id, pr.cnpj1, pr.cnpj2, 'endereco'
FROM pares pr
JOIN localizado_em a1 ON a1.cnpj = pr.cnpj1
JOIN localizado_em a2 ON a2.cnpj = pr.cnpj2 AND a2.endereco_id = a1.endereco_id
""",
)

Q4 = Consulta(
    nome="Q4",
    saltos="1 a 6 (caminho societário de tamanho variável)",
    descricao=(
        "Menor caminho SOCIO|SOCIO_PJ de até 6 saltos entre as empresas de dois coparticipantes, "
        "com todas as arestas anteriores ao certame (Cypher shortestPath × CTE recursiva)"
    ),
    cypher="""
MATCH (e1:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao)<-[:PARTICIPOU_DE]-(e2:Estabelecimento)
WHERE e1.cnpj < e2.cnpj AND left(e1.cnpj, 8) <> left(e2.cnpj, 8)
WITH DISTINCT l, e1, e2
MATCH (e1)-[:PERTENCE_A]->(a:Empresa), (e2)-[:PERTENCE_A]->(b:Empresa)
WHERE a <> b
MATCH p = shortestPath((a)-[:SOCIO|SOCIO_PJ*..6]-(b))
WHERE all(r IN relationships(p) WHERE r.dataEntrada <= l.dataRealizacao)
RETURN DISTINCT l.id AS id, e1.cnpj AS cnpj1, e2.cnpj AS cnpj2, length(p) AS saltos
""",
    sql="""
WITH RECURSIVE pares AS (
  SELECT DISTINCT l.id, l.data_realizacao, e1.cnpj AS cnpj1, e2.cnpj AS cnpj2,
         'E' || e1.cnpj_basico AS origem, 'E' || e2.cnpj_basico AS destino
  FROM licitacao l
  JOIN participou_de p1 ON p1.licitacao_id = l.id
  JOIN participou_de p2 ON p2.licitacao_id = l.id
  JOIN estabelecimento e1 ON e1.cnpj = p1.cnpj
  JOIN estabelecimento e2 ON e2.cnpj = p2.cnpj
  WHERE e1.cnpj < e2.cnpj AND left(e1.cnpj, 8) <> left(e2.cnpj, 8)
    AND e1.cnpj_basico IS NOT NULL AND e2.cnpj_basico IS NOT NULL
    AND e1.cnpj_basico <> e2.cnpj_basico
),
busca (id, data_realizacao, cnpj1, cnpj2, destino, no, visitados, saltos) AS (
  SELECT id, data_realizacao, cnpj1, cnpj2, destino, origem, ARRAY[origem], 0
  FROM pares
  UNION ALL
  SELECT b.id, b.data_realizacao, b.cnpj1, b.cnpj2, b.destino, a.destino_no, b.visitados || a.destino_no,
         b.saltos + 1
  FROM busca b
  JOIN aresta_societaria a ON a.origem_no = b.no
  WHERE b.saltos < 6 AND b.no <> b.destino
    AND a.data_entrada <= b.data_realizacao
    AND a.destino_no <> ALL (b.visitados)
)
SELECT id, cnpj1, cnpj2, min(saltos) AS saltos
FROM busca
WHERE no = destino
GROUP BY id, cnpj1, cnpj2
""",
)

CONSULTAS: list[Consulta] = [Q0, Q1, Q2, Q3, Q4]

# Licitação de referência da Q1: a de mais participantes (desempate pelo id), escolhida no grafo.
CYPHER_LICITACAO_REFERENCIA = """
MATCH (e:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao)
WITH l, count(DISTINCT e) AS n
RETURN l.id AS id, n ORDER BY n DESC, id LIMIT 1
"""
