"""Todo arquivo .py de src/ precisa compilar (2026-09-25) - pega erro de
sintaxe mesmo em módulo que nenhum outro teste importa."""

import py_compile
from pathlib import Path

import pytest

ARQUIVOS = sorted((Path(__file__).resolve().parent.parent / "src").rglob("*.py"))


@pytest.mark.parametrize("arquivo", ARQUIVOS, ids=lambda p: p.name)
def test_arquivo_compila(arquivo):
    py_compile.compile(str(arquivo), doraise=True)
