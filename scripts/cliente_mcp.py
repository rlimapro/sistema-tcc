"""Cliente MCP mínimo: sobe `sarc mcp` por stdio, como um cliente LLM faria, e chama tools.

Uso (imprime um JSON no stdout):
    uv run python scripts/cliente_mcp.py --env-file .env --municipio 087 --desde 2021-01-01 --ate 2021-12-31

Chama `resumo_da_carga` e `licitacoes_com_conluio` e devolve, para cada uma, o tempo e um resumo.
Com `--completo`, devolve também as respostas inteiras.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any


async def conversar(env_file: str, municipio: str, desde: str | None, ate: str | None) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from sarc.cli import main; main()", "--env-file", env_file, "mcp"],
        env={**os.environ},
    )
    saida: dict[str, Any] = {}
    async with stdio_client(params) as (leitura, escrita), ClientSession(leitura, escrita) as sessao:
        await sessao.initialize()
        saida["tools"] = sorted(t.name for t in (await sessao.list_tools()).tools)

        async def chamar(nome: str, argumentos: dict[str, Any]) -> None:
            inicio = time.perf_counter()
            resposta = await sessao.call_tool(nome, argumentos)
            saida[nome] = {
                "segundos": round(time.perf_counter() - inicio, 3),
                "resposta": json.loads(resposta.content[0].text),
            }

        await chamar("resumo_da_carga", {})
        argumentos: dict[str, Any] = {"municipio": municipio}
        if desde:
            argumentos["desde"] = desde
        if ate:
            argumentos["ate"] = ate
        await chamar("licitacoes_com_conluio", argumentos)
    return saida


def resumir(saida: dict[str, Any]) -> dict[str, Any]:
    carga = saida["resumo_da_carga"]["resposta"].get("dados", {})
    conluio = saida["licitacoes_com_conluio"]["resposta"]
    dados = conluio.get("dados", {})
    return {
        "tools": saida["tools"],
        "resumo_da_carga": {
            "segundos": saida["resumo_da_carga"]["segundos"],
            "nos": carga.get("nos"),
            "arestas": carga.get("arestas"),
            "cargas": [
                {
                    k: c.get(k)
                    for k in ("etapa", "status", "municipios", "desde", "ate", "snapshotRfb", "amostra")
                }
                for c in carga.get("cargas", [])
            ],
        },
        "licitacoes_com_conluio": {
            "segundos": saida["licitacoes_com_conluio"]["segundos"],
            "erro": conluio.get("erro"),
            "alertas": len(dados.get("alertas") or []),
            "alertasConfiancaDesconhecida": len(dados.get("alertasConfiancaDesconhecida") or []),
            "chaves": sorted(dados),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--municipio", required=True)
    parser.add_argument("--desde")
    parser.add_argument("--ate")
    parser.add_argument("--completo", action="store_true", help="Inclui as respostas inteiras.")
    args = parser.parse_args()
    saida = asyncio.run(conversar(args.env_file, args.municipio, args.desde, args.ate))
    resultado = {"resumo": resumir(saida)}
    if args.completo:
        resultado["respostas"] = saida
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
