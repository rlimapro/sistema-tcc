"""Configuração do logging. Tudo vai para stderr: o stdout fica livre para o MCP via stdio."""

from __future__ import annotations

import logging
import sys

FORMATO = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configurar_log(nivel: str = "INFO", arquivo: str | None = None) -> None:
    """Configura o logger raiz (stderr e, se pedido, um arquivo). Pode ser chamada de novo."""
    manipuladores: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if arquivo:
        manipuladores.append(logging.FileHandler(arquivo, encoding="utf-8"))
    logging.basicConfig(level=nivel.upper(), format=FORMATO, handlers=manipuladores, force=True)
    # O driver do Neo4j é verboso em DEBUG; só interessa aviso. As notificações do servidor
    # (ex.: "relationship type does not exist" num grafo ainda sem REPRESENTA) poluem o stderr do MCP.
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
