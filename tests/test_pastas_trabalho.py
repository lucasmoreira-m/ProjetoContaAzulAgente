import os

import pytest

from src.utils import pastas_trabalho


@pytest.mark.parametrize("nome, esperado", [
    ("bruno.silva@empresa.com.br", "bruno.silva"),
    ("Ana", "Ana"),
    ("  ", "geral"),
    (None, "geral"),
    ("../../etc", "etc"),
    ("a/b\\c", "abc"),
    ("..", "geral"),
])
def test_nome_pasta_usuario_e_seguro(nome, esperado):
    assert pastas_trabalho.nome_pasta_usuario(nome) == esperado


@pytest.fixture
def entrada(tmp_path, monkeypatch):
    monkeypatch.setattr(pastas_trabalho.config, "PASTA_ENTRADA", str(tmp_path / "entrada"))
    return tmp_path / "entrada"


def test_upload_vai_para_subpasta_de_quem_enviou(entrada):
    destino = pastas_trabalho.caminho_destino_upload("fatura.pdf", "Ana")
    assert destino == os.path.join(str(entrada), "Ana", "fatura.pdf")


def test_upload_sem_nome_vai_para_raiz(entrada):
    assert pastas_trabalho.caminho_destino_upload("fatura.pdf", "geral") == os.path.join(str(entrada), "fatura.pdf")


def test_upload_ignora_caminho_vindo_do_navegador(entrada):
    destino = pastas_trabalho.caminho_destino_upload("..\\..\\fatura.pdf", "geral")
    assert destino == os.path.join(str(entrada), "fatura.pdf")


def test_upload_nao_sobrescreve_arquivo_aguardando(entrada):
    (entrada / "Ana").mkdir(parents=True)
    (entrada / "Ana" / "fatura.pdf").write_bytes(b"%PDF")
    destino = pastas_trabalho.caminho_destino_upload("fatura.pdf", "Ana")
    assert os.path.basename(destino) == "fatura (2).pdf"


def test_upload_recusa_tipo_nao_aceito(entrada):
    with pytest.raises(ValueError):
        pastas_trabalho.caminho_destino_upload("planilha.xlsx", "Ana")
