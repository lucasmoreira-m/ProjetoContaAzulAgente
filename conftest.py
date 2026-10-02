import os
import sys
from pathlib import Path

import pytest

# Reestruturação para src/ (2026-09-03): todo import do projeto agora usa
# caminho absoluto a partir do pacote "src" (ex: "from src.core import
# config", "from src.agentes import agente_folha") - por isso o que
# precisa estar no sys.path é a RAIZ DO PROJETO (a pasta que CONTÉM
# "src/"), não a própria pasta "src/". Como este conftest.py mora na raiz,
# `Path(__file__).parent` já é exatamente essa pasta - garante que os
# testes importem "src.*" independentemente do diretório de onde o pytest
# for chamado.
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _ambiente_padrao_nos_testes(monkeypatch):
    """Os testes não podem depender de existir um .env (2026-10-02): numa
    cópia limpa do repositório (GitHub, CI) CONTA_AZUL_AMBIENTE não existe e
    as páginas do painel quebrariam ao mostrar o selo de ambiente. Só
    preenche quando está vazia - com um .env presente, nada muda."""
    if not os.environ.get("CONTA_AZUL_AMBIENTE"):
        monkeypatch.setenv("CONTA_AZUL_AMBIENTE", "teste")
