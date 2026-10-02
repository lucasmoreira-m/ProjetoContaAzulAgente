import os
import threading

import pytest

from src.agentes import monitor_pasta
from src.agentes import processador


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    for nome in ("entrada", "processados", "revisao", "descartados"):
        (tmp_path / nome).mkdir()
        monkeypatch.setattr(monitor_pasta.config, f"PASTA_{nome.upper()}", str(tmp_path / nome))
    monkeypatch.setattr(processador, "CAMINHO_TRAVA_ARQUIVO", str(tmp_path / "processamento.lock"))
    monkeypatch.setattr(processador.config, "PROCESSADOR", "local")
    monkeypatch.setattr(monitor_pasta.verificacao_duplicidade, "registrar_arquivo_processado", lambda c, r: None)
    monkeypatch.setattr(monitor_pasta.historico_lancamentos, "registrar_lancamentos_de_resultado", lambda *a: None)
    return tmp_path


def test_processa_o_que_esta_na_entrada_e_move_cada_arquivo(pastas, monkeypatch):
    (pastas / "entrada" / "Ana").mkdir()
    (pastas / "entrada" / "Ana" / "fatura.pdf").write_bytes(b"%PDF")
    (pastas / "entrada" / "solto.pdf").write_bytes(b"%PDF")

    def _processar_falso(caminho, usuario):
        sucesso = usuario == "Ana"
        return {"sucesso": sucesso, "motivo": "ok" if sucesso else "revisar", "detalhes": {"x": 1}}

    monkeypatch.setattr(monitor_pasta, "processar_documento_com_classificacao", _processar_falso)

    resultados = processador.processar_entrada(acionado_por="teste")

    por_arquivo = {r["arquivo"]: r for r in resultados}
    assert por_arquivo["fatura.pdf"]["destino"] == "processados"
    assert por_arquivo["fatura.pdf"]["usuario"] == "Ana"
    assert por_arquivo["solto.pdf"]["destino"] == "revisao"
    assert por_arquivo["solto.pdf"]["usuario"] == "geral"
    assert monitor_pasta.listar_arquivos_com_usuario() == []
    assert any(n.endswith("_fatura.pdf") for n in os.listdir(pastas / "processados"))
    assert any(n.endswith("_solto_dados.json") for n in os.listdir(pastas / "revisao"))


def test_erro_num_arquivo_nao_derruba_o_lote(pastas, monkeypatch):
    (pastas / "entrada" / "a.pdf").write_bytes(b"%PDF")
    (pastas / "entrada" / "b.pdf").write_bytes(b"%PDF")

    def _processar_falso(caminho, usuario):
        if caminho.endswith("a.pdf"):
            raise RuntimeError("falha")
        return {"sucesso": True, "motivo": "ok", "detalhes": {"x": 1}}

    monkeypatch.setattr(monitor_pasta, "processar_documento_com_classificacao", _processar_falso)

    resultados = processador.processar_entrada(acionado_por="teste")

    assert {r["arquivo"]: r["destino"] for r in resultados} == {"a.pdf": "revisao", "b.pdf": "processados"}


def test_entrada_vazia_devolve_lista_vazia(pastas):
    assert processador.processar_entrada(acionado_por="teste") == []


def test_segundo_clique_durante_processamento_e_recusado(pastas, monkeypatch):
    (pastas / "entrada" / "a.pdf").write_bytes(b"%PDF")
    iniciou, liberar = threading.Event(), threading.Event()

    def _processar_lento(caminho, usuario):
        iniciou.set()
        liberar.wait(5)
        return {"sucesso": True, "motivo": "ok", "detalhes": {"x": 1}}

    monkeypatch.setattr(monitor_pasta, "processar_documento_com_classificacao", _processar_lento)
    primeiro = threading.Thread(target=processador.processar_entrada, args=("pessoa 1",))
    primeiro.start()
    try:
        assert iniciou.wait(5)
        with pytest.raises(processador.ProcessamentoEmAndamento):
            processador.processar_entrada(acionado_por="pessoa 2")
    finally:
        liberar.set()
        primeiro.join(5)

    # Terminado o primeiro, a trava é liberada.
    assert processador.processar_entrada(acionado_por="pessoa 2") == []


def test_modo_azure_ainda_nao_disponivel(pastas, monkeypatch):
    monkeypatch.setattr(processador.config, "PROCESSADOR", "azure")
    with pytest.raises(NotImplementedError):
        processador.processar_entrada(acionado_por="teste")


def test_modo_desconhecido_e_recusado(pastas, monkeypatch):
    monkeypatch.setattr(processador.config, "PROCESSADOR", "nuvem")
    with pytest.raises(ValueError):
        processador.processar_entrada(acionado_por="teste")
