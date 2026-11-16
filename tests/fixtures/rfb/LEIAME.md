# Fixtures da RFB

Linhas reais do snapshot 2026-08 (`mvp-v1/data/rfb/2026-08`) de 4 raízes de CNPJ, reduzidas:

- `00085822`: sócio PJ (holding `13261413`, documento com 8 dígitos) com representante 05 e 3 sócios PF;
- `00934286`: sócio PJ estrangeiro (37) com procurador (17) e 4 filiais, só a matriz e a 0002 entram nos testes;
- `22705562`: sócio menor (30, mascarado para 22) representado pelo pai (15, não vira aresta);
- `04942400`: sócio estrangeiro PF (identificador 3) com procurador (17).

Nomes e CPFs mascarados de pessoas físicas foram trocados por fictícios, de forma consistente (a mesma
pessoa como sócia e como representante tem o mesmo nome fictício). CNPJ, razão social e contato de PJ são
os reais (dados públicos de empresa). `Socios1` traz variantes sintéticas: documento PJ com 14 dígitos
(formato de 2023-05), data `00000000`, documento PJ curto e uma linha malformada.
