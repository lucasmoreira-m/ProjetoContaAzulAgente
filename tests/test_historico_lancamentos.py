import json
import os

from src.utils import historico_lancamentos as hl


def test_registrar_e_listar_documento_comum(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.config, "PASTA_PROCESSADOS", str(tmp_path))
    caminho_pdf = tmp_path / "20260901_fatura.pdf"
    caminho_pdf.write_bytes(b"conteudo falso")

    hl.registrar_lancamento(
        str(caminho_pdf),
        origem="manual",
        tipo="comum",
        protocolo="proto-1",
        descricao="Fatura teste",
        valor=100.0,
        data_vencimento="2026-09-10",
        data_competencia="2026-09-10",
        categoria={"id": "cat-1", "nome": "Categoria X"},
        contato={"id": "cont-1", "nome": "Fornecedor X"},
        rateio=[{"centro_custo_id": "cc-1", "centro_custo_nome": "Financeiro", "valor": 100.0}],
    )

    historico = hl.listar_historico()
    assert len(historico) == 1
    assert historico[0]["protocolo"] == "proto-1"
    assert historico[0]["categoria"]["nome"] == "Categoria X"
    assert historico[0]["_caminho_json"].endswith("_lancamento.json")


def test_registrar_lancamentos_de_resultado_formato_comum(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.config, "PASTA_PROCESSADOS", str(tmp_path))
    caminho_pdf = tmp_path / "doc.pdf"
    caminho_pdf.write_bytes(b"x")

    detalhes_comum = {
        "protocolo": "proto-comum",
        "descricao": "desc",
        "valor": 50.0,
        "data_vencimento": "2026-09-10",
        "categoria": {"id": "c1", "nome": "Cat"},
        "contato": {"id": "p1", "nome": "Pessoa"},
    }
    hl.registrar_lancamentos_de_resultado(str(caminho_pdf), "automatico", detalhes_comum)

    historico = hl.listar_historico()
    assert len(historico) == 1
    assert historico[0]["tipo"] == "comum"
    assert historico[0]["origem"] == "automatico"


def test_registrar_lancamentos_de_resultado_persiste_auditoria_camada2(tmp_path, monkeypatch):
    # Auditoria do Funil de Decisão Contábil (2026-09-04, pedido explícito
    # do usuário): um lançamento da Camada 2 (inferido do ERP, sem nenhuma
    # aprovação humana anterior) precisa ficar identificável no Histórico
    # local depois - painel_revisao.py lê "camada_decisao" e
    # "camada2_evidencia_qtd" para montar a seção "Auditoria Camada 2".
    monkeypatch.setattr(hl.config, "PASTA_PROCESSADOS", str(tmp_path))
    caminho_pdf = tmp_path / "doc_erp.pdf"
    caminho_pdf.write_bytes(b"x")

    detalhes_camada2 = {
        "protocolo": "proto-erp",
        "descricao": "desc",
        "valor": 300.0,
        "data_vencimento": "2026-09-10",
        "categoria": {"id": "cat-erp", "nome": "Manutenção"},
        "contato": {"id": "p1", "nome": "Fornecedor Novo"},
        "origem_categoria": "inferido_do_erp",
        "camada_decisao": "camada2_erp_inferido",
        "camada2_evidencia_qtd": 4,
    }
    hl.registrar_lancamentos_de_resultado(str(caminho_pdf), "automatico", detalhes_camada2)

    historico = hl.listar_historico()
    assert len(historico) == 1
    assert historico[0]["camada_decisao"] == "camada2_erp_inferido"
    assert historico[0]["camada2_evidencia_qtd"] == 4
    assert historico[0]["origem_categoria"] == "inferido_do_erp"


def test_registrar_lancamentos_de_resultado_formato_folha_multiplas_tabelas(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.config, "PASTA_PROCESSADOS", str(tmp_path))
    caminho_pdf = tmp_path / "folha.pdf"
    caminho_pdf.write_bytes(b"x")

    detalhes_folha = {
        "inss_alfa_0": {
            "sucesso": True,
            "detalhes": {"protocolo": "proto-inss", "valor": 10.0, "categoria": {"id": "c1", "nome": "INSS"}, "contato": {"id": "p1", "nome": "Ministerio"}},
        },
        "fgts_alfa_1": {
            "sucesso": True,
            "detalhes": {"protocolo": "proto-fgts", "valor": 20.0, "categoria": {"id": "c2", "nome": "FGTS"}, "contato": {"id": "p2", "nome": "FGTS"}},
        },
        "prolabore_sembanco_2": {
            "sucesso": False,
            "ignorado_propositalmente": True,
            "detalhes": {},
        },
    }
    hl.registrar_lancamentos_de_resultado(str(caminho_pdf), "automatico", detalhes_folha)

    historico = hl.listar_historico()
    protocolos = {item["protocolo"] for item in historico}
    assert protocolos == {"proto-inss", "proto-fgts"}
    assert all(item["tipo"] == "folha" for item in historico)


def test_atualizar_apos_edicao_reescreve_arquivo(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.config, "PASTA_PROCESSADOS", str(tmp_path))
    caminho_pdf = tmp_path / "doc.pdf"
    caminho_pdf.write_bytes(b"x")

    hl.registrar_lancamento(
        str(caminho_pdf), origem="manual", tipo="comum", protocolo="p1",
        descricao="original", valor=10.0, data_vencimento="2026-01-01",
        data_competencia="2026-01-01", categoria=None, contato=None, rateio=None,
    )
    caminho_json = hl.caminho_lancamento_json(str(caminho_pdf))
    registro = json.loads(open(caminho_json, encoding="utf-8").read())
    registro["_caminho_json"] = caminho_json
    registro["descricao"] = "editado"

    hl.atualizar_apos_edicao(caminho_json, registro)

    with open(caminho_json, encoding="utf-8") as f:
        atualizado = json.load(f)
    assert atualizado["descricao"] == "editado"
    assert "_caminho_json" not in atualizado
