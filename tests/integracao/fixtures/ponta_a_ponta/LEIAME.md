# Fixture do teste ponta a ponta (T08)

Dados reais reduzidos de um município e um ano, para rodar ETL 1 → ETL 2 → conluio → tool MCP.

- `tce/cache/`: o cache do TCE-CE de Itatira (087) em 2021, copiado de `tests/fixtures/tce/cache`
  (5 baldes: municípios, processos, licitantes, contratos e contratados). Inclui o certame
  `087|1308022021PE` (17 licitantes).
- `rfb/2026-08/`: as linhas dos CSVs reais do snapshot 2026-08 (`mvp-v1/data/rfb/2026-08`) cujo texto contém
  uma das 28 raízes de CNPJ dos licitantes PJ acima, achadas com `grep -a -F` nos 10 arquivos de cada tipo
  (`Empresas0`, `Estabelecimentos0` e `Socios0` reúnem o resultado dos 10). `Municipios` é o arquivo inteiro.
  Nomes de sócios e representantes pessoas físicas foram trocados por `PESSOA FICTICIA PPnn`, de forma
  consistente (mesmo nome e mesmo CPF mascarado, mesmo nome fictício). CNPJ, razão social e contato de PJ
  são os reais (dados públicos de empresa).

Resultado esperado (conferido em 24/10/2026): 4 licitações, 31 participações, 30 arestas SOCIO e 2 alertas
de nível principal por endereço declarado no certame (tabela 505) entre `27499707000140` e `32490833000174`.
