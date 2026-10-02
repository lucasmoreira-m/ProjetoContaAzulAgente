"""
Operações nas pastas de trabalho (entrada/, revisao/) usadas pela tela -
sem depender de framework de interface, para servir tanto o painel
Streamlit quanto o painel FastAPI durante a migração (2026-09-30).
"""

from __future__ import annotations

import glob
import json
import os
import re

from src.core import config
from src.agentes.monitor_pasta import EXTENSOES_ACEITAS


def nome_pasta_usuario(nome: str | None) -> str:
    """Nome de subpasta seguro a partir do nome/e-mail de quem enviou:
    'bruno.silva@empresa.com.br' -> 'bruno.silva'. Vazio -> 'geral'
    (raiz da entrada, como já era para arquivo solto)."""
    nome = (nome or "").split("@")[0]
    nome = re.sub(r"[^\w .-]", "", nome, flags=re.UNICODE).strip(" .")
    return nome or "geral"


def caminho_destino_upload(nome_arquivo: str, usuario: str) -> str:
    """Onde salvar um arquivo enviado pela página. Usa só o nome do arquivo
    (nunca um caminho vindo do navegador) e não sobrescreve um arquivo que
    já esteja aguardando com o mesmo nome."""
    nome_arquivo = os.path.basename(nome_arquivo.replace("\\", "/"))
    if not nome_arquivo.lower().endswith(EXTENSOES_ACEITAS):
        raise ValueError(f"Tipo de arquivo não aceito: {nome_arquivo}")

    pasta = config.PASTA_ENTRADA if usuario == "geral" else os.path.join(config.PASTA_ENTRADA, usuario)
    os.makedirs(pasta, exist_ok=True)

    base, ext = os.path.splitext(nome_arquivo)
    destino = os.path.join(pasta, nome_arquivo)
    contador = 2
    while os.path.exists(destino):
        destino = os.path.join(pasta, f"{base} ({contador}){ext}")
        contador += 1
    return destino


def listar_pendencias() -> list[dict]:
    """Itens aguardando revisão: cada revisao/*_dados.json com o documento
    original ao lado (mesmo nome-base), se ainda existir."""
    pendencias = []
    for caminho_json in glob.glob(os.path.join(config.PASTA_REVISAO, "*_dados.json")):
        with open(caminho_json, "r", encoding="utf-8") as f:
            info = json.load(f)
        base = caminho_json.replace("_dados.json", "")
        doc_original = next(
            (base + ext for ext in (".pdf", ".jpg", ".jpeg", ".png") if os.path.exists(base + ext)), None,
        )
        pendencias.append({"caminho_json": caminho_json, "caminho_documento": doc_original, "info": info})
    return pendencias
