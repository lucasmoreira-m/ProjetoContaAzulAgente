import json
import os
import threading

import pytest

from src.agentes import revisao

CADASTROS = {
    "categorias": [{"id": "cat-1", "nome": "Energia elétrica"}, {"id": "cat-2", "nome": "Telefonia e internet"}],
    "pessoas": [{"id": "p-1", "nome": "Energia Exemplo Distribuição"}, {"id": "p-2", "nome": "Telecom Exemplo"}],
    "centros_custo": [{"id": "cc-1", "nome": "Administrativo"}, {"id": "cc-2", "nome": "Obras"}],
    "contas_financeiras": [{"id": "cf-1", "nome": "Banco Alfa"}, {"id": "cf-2", "nome": "Beta"}],
}

DADOS_VIVO = {
    "tipo_documento": "conta_fixa", "fornecedor": "Telecom Exemplo", "cnpj": "22.333.444/0001-55",
    "valor_total": 300.0, "data_vencimento": "10/10/2026", "categoria_sugerida": "Telefonia",
    "descricao": "Fatura Telecom Exemplo 09/2026", "palavras_chave_identificadas": ["internet"],
    "rateio_centro_custo_documento": [
        {"centro_custo": "Administrativo", "valor": 200.0},
        {"centro_custo": "Obras", "valor": 100.0},
        {"centro_custo": "Diretoria", "valor": 0.0},
    ],
}


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    for nome in ("entrada", "processados", "revisao", "descartados"):
        (tmp_path / nome).mkdir()
        monkeypatch.setattr(revisao.config, f"PASTA_{nome.upper()}", str(tmp_path / nome))
    return tmp_path


def _criar_item(pastas, dados, nome="20260930_101500_telecom"):
    (pastas / "revisao" / f"{nome}.pdf").write_bytes(b"%PDF")
    (pastas / "revisao" / f"{nome}_relatorio.txt").write_text("relatorio", encoding="utf-8")
    (pastas / "revisao" / f"{nome}_dados.json").write_text(json.dumps({
        "documento_original": f"{nome}.pdf", "enviado_por": "Ana", "processado_em": "30/09/2026 10:15:00",
        "sucesso": False, "motivo": "Contato não encontrado", "dados": dados,
    }), encoding="utf-8")
    return nome


@pytest.fixture
def conta_azul_falsa(monkeypatch):
    chamadas = {"criar": [], "duplicata": None, "confirmados": [], "diretrizes": [], "livres": []}
    monkeypatch.setattr(revisao, "obter_access_token", lambda: "token")
    monkeypatch.setattr(revisao.verificacao_duplicidade, "verificar_lancamento_similar_existe",
                        lambda *a, **k: chamadas["duplicata"])

    def _criar(token, dados, categoria_id, contato_id, data_iso, rateio_centro_custo=None, conta_financeira_id=None, **extra):
        chamadas["criar"].append({"valor": dados["valor_total"], "categoria_id": categoria_id, "contato_id": contato_id,
                                  "data": data_iso, "rateio": rateio_centro_custo, "conta": conta_financeira_id,
                                  "descricao": dados.get("descricao"), **extra})
        return 200, {"protocolo": f"PROT-{len(chamadas['criar'])}", "id": "lanc-1"}

    monkeypatch.setattr(revisao, "criar_lancamento", _criar)
    monkeypatch.setattr(revisao.fornecedores_registry, "registrar_confirmado", lambda **k: chamadas["confirmados"].append(k))
    monkeypatch.setattr(revisao.historico_centros_funcionarios, "registrar_diretriz_global_texto",
                        lambda palavra, **k: chamadas["diretrizes"].append((palavra, k)))
    monkeypatch.setattr(revisao.historico_centros_funcionarios, "registrar_diretriz_livre",
                        lambda tipo, texto: chamadas["livres"].append((tipo, texto)))
    monkeypatch.setattr(revisao.historico_centros_funcionarios, "buscar_diretrizes_globais_texto", lambda: {})
    chamadas.update(bancos=[], maturacao=[], centros_funcionario=[])
    monkeypatch.setattr(revisao.fornecedores_registry, "registrar_banco_pagamento_sem_boleto",
                        lambda cnpj, banco, origem: chamadas["bancos"].append((cnpj, banco)))
    monkeypatch.setattr(revisao.agente_folha, "registrar_aprovacao_manual_padrao",
                        lambda tipo, nota: chamadas["maturacao"].append((tipo, nota)))
    monkeypatch.setattr(revisao.historico_centros_funcionarios, "registrar_centro_custo_funcionario",
                        lambda nome, cid, cnome: chamadas["centros_funcionario"].append((nome, cid)))
    return chamadas


FORM_VIVO = {
    "valor": "300,00", "data_vencimento": "10/10/2026", "categoria_id": "cat-2", "contato_id": "p-2",
    "conta_financeira_id": "cf-1", "palavras_chave": "internet", "orientacao": "",
    "rateio_0_incluir": "on", "rateio_0_centro_id": "cc-1", "rateio_0_valor": "200,00",
    "rateio_1_incluir": "on", "rateio_1_centro_id": "cc-2", "rateio_1_valor": "100,00",
    "rateio_2_incluir": "on", "rateio_2_centro_id": "cc-1", "rateio_2_valor": "0,00",
}


def test_tipo_pendencia():
    assert revisao.tipo_pendencia({"registro_anterior": {}}) == "duplicado"
    assert revisao.tipo_pendencia({"tipo_documento": "nfse"}) == "nfse"
    assert revisao.tipo_pendencia({"tipo_documento": "conta_fixa"}) == "comum"
    assert revisao.tipo_pendencia({"FGTS": {"sucesso": False}}) == "folha"


@pytest.mark.parametrize("texto, esperado", [("1.234,56", 1234.56), ("300", 300.0), ("300.5", 300.5), ("R$ 10,00", 10.0)])
def test_parse_valor(texto, esperado):
    assert revisao.parse_valor(texto) == esperado


def test_formulario_pre_seleciona_sugestoes(conta_azul_falsa):
    form = revisao.formulario_comum(DADOS_VIVO, CADASTROS)
    v = form["valores"]
    assert v["categoria_id"] == "cat-2"          # "Telefonia" contido em "Telefonia e internet"
    assert v["contato_id"] == "p-2"              # "Telecom Exemplo" contido em "Telecom Exemplo"
    assert v["conta_financeira_id"] == "cf-1"    # sem sugestão: primeira conta (igual ao painel antigo)
    assert v["valor"] == "300,00"
    linhas = {l["nome_doc"]: l for l in form["linhas_rateio"]}
    assert linhas["Administrativo"]["incluir"] and linhas["Administrativo"]["centro_id"] == "cc-1"
    assert not linhas["Diretoria"]["incluir"]    # sem match exato: desmarcada para conferir
    assert not form["mostrar_fallback"]


def test_formulario_reaproveita_o_que_foi_digitado(conta_azul_falsa):
    enviado = dict(FORM_VIVO, valor="299,90", rateio_1_incluir="")
    form = revisao.formulario_comum(DADOS_VIVO, CADASTROS, enviado)
    assert form["valores"]["valor"] == "299,90"
    assert not form["linhas_rateio"][1]["incluir"]


def test_aprovar_lanca_move_e_registra_quem_aprovou(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_VIVO)

    resultado = revisao.aprovar_comum(item_id, FORM_VIVO, "Bruno", CADASTROS)

    assert resultado == {"status": "lancado", "protocolo": "PROT-1", "reextraidos": 0}
    criado = conta_azul_falsa["criar"][0]
    assert criado["valor"] == 300.0 and criado["categoria_id"] == "cat-2" and criado["contato_id"] == "p-2"
    assert criado["data"] == "2026-10-10" and criado["conta"] == "cf-1"
    # a linha de R$ 0,00 fica de fora (a Conta Azul recusa rateio zerado)
    assert [(l["centro_custo_id"], l["valor"]) for l in criado["rateio"]] == [("cc-1", 200.0), ("cc-2", 100.0)]

    assert os.listdir(pastas / "revisao") in ([], [".travas"])
    processados = os.listdir(pastas / "processados")
    assert any(n.endswith("_telecom.pdf") for n in processados)
    assert any(n.endswith("_telecom_relatorio.txt") for n in processados)
    registro = json.loads(next((pastas / "processados").glob("*_lancamento.json")).read_text(encoding="utf-8"))
    assert registro["aprovado_por"] == "Bruno" and registro["origem"] == "manual" and registro["protocolo"] == "PROT-1"

    assert conta_azul_falsa["confirmados"][0]["contato_id"] == "p-2"
    assert conta_azul_falsa["diretrizes"] == [("internet", {"categoria_nome": "Telefonia e internet",
                                                           "conta_financeira_nome": "Banco Alfa", "centro_custo_nome": None})]


def test_duplicata_nao_lanca_ate_confirmar(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_VIVO)
    conta_azul_falsa["duplicata"] = {"id": "x-9", "status": "PAGO"}

    resultado = revisao.aprovar_comum(item_id, FORM_VIVO, "Bruno", CADASTROS)
    assert resultado == {"status": "duplicata", "duplicata": {"id": "x-9", "status": "PAGO"}}
    assert conta_azul_falsa["criar"] == []
    assert (pastas / "revisao" / f"{item_id}_dados.json").exists()

    resultado = revisao.aprovar_comum(item_id, FORM_VIVO, "Bruno", CADASTROS, forcar_duplicata=True)
    assert resultado["status"] == "lancado"
    assert len(conta_azul_falsa["criar"]) == 1


def test_segunda_aprovacao_do_mesmo_item_nao_lanca_de_novo(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_VIVO)
    assert revisao.aprovar_comum(item_id, FORM_VIVO, "Bruno", CADASTROS)["status"] == "lancado"
    assert revisao.aprovar_comum(item_id, FORM_VIVO, "Ana", CADASTROS) == {"status": "ja_tratado"}
    assert len(conta_azul_falsa["criar"]) == 1


def test_duas_pessoas_ao_mesmo_tempo_so_uma_lanca(pastas, conta_azul_falsa, monkeypatch):
    item_id = _criar_item(pastas, DADOS_VIVO)
    dentro, liberar = threading.Event(), threading.Event()
    criar_original = revisao.criar_lancamento

    def _criar_lento(*a, **k):
        dentro.set()
        liberar.wait(5)
        return criar_original(*a, **k)

    monkeypatch.setattr(revisao, "criar_lancamento", _criar_lento)
    resultados = {}
    primeira = threading.Thread(target=lambda: resultados.update(a=revisao.aprovar_comum(item_id, FORM_VIVO, "Bruno", CADASTROS)))
    primeira.start()
    try:
        assert dentro.wait(5)
        with pytest.raises(revisao.ItemEmUso):
            revisao.aprovar_comum(item_id, FORM_VIVO, "Ana", CADASTROS)
    finally:
        liberar.set()
        primeira.join(5)
    assert resultados["a"]["status"] == "lancado"
    assert len(conta_azul_falsa["criar"]) == 1


def test_centro_de_custo_unico_quando_documento_nao_tem_rateio(pastas, conta_azul_falsa):
    dados = dict(DADOS_VIVO, rateio_centro_custo_documento=[])
    item_id = _criar_item(pastas, dados)
    form = {k: v for k, v in FORM_VIVO.items() if not k.startswith("rateio_")}
    form["centro_fallback_id"] = "cc-2"

    revisao.aprovar_comum(item_id, form, "Bruno", CADASTROS)

    assert conta_azul_falsa["criar"][0]["rateio"] == [
        {"centro_custo_id": "cc-2", "centro_custo_nome": "Obras", "percentual": None, "valor": 300.0}]
    assert conta_azul_falsa["diretrizes"][0][1]["centro_custo_nome"] == "Obras"


def test_data_invalida_devolve_erro_sem_lancar(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_VIVO)
    resultado = revisao.aprovar_comum(item_id, dict(FORM_VIVO, data_vencimento="amanhã"), "Bruno", CADASTROS)
    assert resultado["status"] == "erro"
    assert conta_azul_falsa["criar"] == []


def test_orientacao_livre_e_gravada_e_reextrai_pendentes(pastas, conta_azul_falsa, monkeypatch):
    item_id = _criar_item(pastas, DADOS_VIVO)
    chamadas = []
    monkeypatch.setattr(revisao, "reextrair_pendentes_com_nova_diretriz", lambda tipo, ignorar: chamadas.append((tipo, ignorar)) or 2)

    resultado = revisao.aprovar_comum(item_id, dict(FORM_VIVO, orientacao="Telecom Exemplo sempre na filial SP"), "Bruno", CADASTROS)

    assert resultado["reextraidos"] == 2
    assert conta_azul_falsa["livres"] == [("comum", "Telecom Exemplo sempre na filial SP")]
    assert chamadas[0][0] == "comum"


def test_descartar_move_para_descartados(pastas):
    item_id = _criar_item(pastas, DADOS_VIVO)
    assert revisao.descartar(item_id, "Bruno") is True
    assert sorted(n for n in os.listdir(pastas / "descartados")) and not (pastas / "revisao" / f"{item_id}_dados.json").exists()
    assert revisao.descartar(item_id, "Bruno") is False


@pytest.mark.parametrize("item_id", ["../revisao/x", "..", "", ".travas", "nao_existe"])
def test_item_id_invalido_nao_resolve(pastas, item_id):
    assert revisao.caminho_json_do_item(item_id) is None


def test_partes_do_motivo_separa_medidas_de_seguranca():
    partes = revisao.partes_do_motivo(
        "Contato não encontrado | MEDIDA DE SEGURANÇA: valor acima do teto | POSSÍVEL MANIPULAÇÃO DO DOCUMENTO: texto oculto")
    assert [p["tipo"] for p in partes] == ["seguranca", "manipulacao", "motivo"]
    assert partes[0]["texto"] == "valor acima do teto"
    assert partes[2]["texto"] == "Contato não encontrado"


# ---------------------------------------------------------------------------
# NFS-e
# ---------------------------------------------------------------------------
CADASTROS_NFSE = dict(CADASTROS, categorias=CADASTROS["categorias"] + [{"id": "cat-3", "nome": "Plano de saúde"}],
                      pessoas=CADASTROS["pessoas"] + [{"id": "p-3", "nome": "Saúde Exemplo S.A."}])

DADOS_NFSE = {
    "tipo_documento": "nfse", "numero_nota": "10000001", "fornecedor_nome": "Saúde Exemplo", "fornecedor_cnpj": "44.555.666/0001-77",
    "categoria_sugerida": "Plano de saúde", "valor_total": 1000.0, "valor_imposto": 50.0, "valor_liquido": 950.0,
    "data_vencimento_sugerida": "2026-10-05", "data_competencia": "2026-09-01", "tem_boleto": False, "banco_sugerido": None,
    "descricao": "NFS-e 10000001 - Plano de saúde 09/2026", "discriminacao_servicos": "Plano", "codigo_servico": "4.22",
}

FORM_NFSE = {
    "valor": "1.000,00", "data_vencimento": "05/10/2026", "data_competencia": "01/09/2026", "valor_liquido": "950,00",
    "descricao": "NFS-e 10000001 - Plano de saúde (editada)", "categoria_id": "cat-3", "contato_id": "p-3",
    "banco": "beta", "orientacao": "",
}


def test_formulario_nfse_pre_seleciona_e_pede_banco(conta_azul_falsa):
    form = revisao.formulario_nfse(DADOS_NFSE, CADASTROS_NFSE)
    v = form["valores"]
    assert v["categoria_id"] == "cat-3" and v["contato_id"] == "p-3"
    assert v["data_vencimento"] == "05/10/2026" and v["valor_liquido"] == "950,00"
    assert v["descricao"] == DADOS_NFSE["descricao"]
    assert form["modo_banco"] == "escolher"


def test_aprovar_nfse_usa_banco_escolhido_e_aprende(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_NFSE, nome="20260930_saude")

    resultado = revisao.aprovar_nfse(item_id, FORM_NFSE, "Bruno", CADASTROS_NFSE)

    assert resultado["status"] == "lancado"
    criado = conta_azul_falsa["criar"][0]
    assert criado["conta"] == revisao.regras_nfse.CONTA_FINANCEIRA_BETA_ID
    assert criado["forma_pagamento"] == "transferência bancária"
    assert criado["valor_liquido"] == 950.0 and criado["data_competencia"] == "2026-09-01"
    assert criado["descricao"] == "NFS-e 10000001 - Plano de saúde (editada)"
    assert conta_azul_falsa["bancos"] == [("44.555.666/0001-77", "beta")]
    registro = json.loads(next((pastas / "processados").glob("*_lancamento.json")).read_text(encoding="utf-8"))
    assert registro["aprovado_por"] == "Bruno"


def test_nfse_sem_banco_escolhido_nao_lanca(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_NFSE, nome="20260930_saude")
    resultado = revisao.aprovar_nfse(item_id, dict(FORM_NFSE, banco=""), "Bruno", CADASTROS_NFSE)
    assert resultado["status"] == "erro" and conta_azul_falsa["criar"] == []


def test_nfse_com_boleto_paga_no_alfa_e_nao_grava_banco(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, dict(DADOS_NFSE, tem_boleto=True), nome="20260930_saude")
    revisao.aprovar_nfse(item_id, dict(FORM_NFSE, banco="beta"), "Bruno", CADASTROS_NFSE)
    criado = conta_azul_falsa["criar"][0]
    assert criado["conta"] == revisao.regras_nfse.CONTA_FINANCEIRA_ALFA_ID
    assert criado["forma_pagamento"] == "boleto bancário"
    assert conta_azul_falsa["bancos"] == []


def test_nfse_duplicata_so_lanca_com_confirmacao(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_NFSE, nome="20260930_saude")
    conta_azul_falsa["duplicata"] = {"id": "d-1", "status": "EM_ABERTO"}
    assert revisao.aprovar_nfse(item_id, FORM_NFSE, "Bruno", CADASTROS_NFSE)["status"] == "duplicata"
    assert revisao.aprovar_nfse(item_id, FORM_NFSE, "Bruno", CADASTROS_NFSE, forcar_duplicata=True)["status"] == "lancado"
    assert len(conta_azul_falsa["criar"]) == 1


# ---------------------------------------------------------------------------
# Folha de pagamento
# ---------------------------------------------------------------------------
def _tabela(tipo, valor, **extra):
    return {"sucesso": False, "motivo": f"{tipo} pendente", "detalhes": {"tipo": tipo}, **extra, "dados_para_edicao": {
        "valor_total": valor, "data_vencimento_sugerida": "2026-10-07", "data_competencia_sugerida": "2026-09-01",
        "categoria_nome_sugerida": "Energia", "fornecedor_nome_sugerido": "Energia Exemplo", "tipo_lancamento": tipo,
        "rateio_doc": [{"centro_custo": "Obras", "valor": valor}], "fonte_centro_custo": "revisao_manual",
        "descricao_sugerida": f"{tipo.upper()} 09/2026", "forma_pagamento_sugerida": "boleto", "competencia": "09/2026",
    }}


DADOS_FOLHA = {
    "FGTS": _tabela("fgts", 500.0),
    "INSS": _tabela("inss", 800.0),
    "IRRF": _tabela("irrf", 90.0, ignorado_propositalmente=True),
}


def _form_folha(valor):
    return {"valor": valor, "data_vencimento": "07/10/2026", "data_competencia": "01/09/2026", "descricao": "",
            "categoria_id": "cat-1", "contato_id": "p-1", "conta_financeira_id": "cf-1", "orientacao": "conferir o TRCT",
            "rateio_0_incluir": "on", "rateio_0_centro_id": "cc-2", "rateio_0_valor": valor}


def test_formulario_folha_lista_tabelas(conta_azul_falsa):
    form = revisao.formulario_folha(DADOS_FOLHA, CADASTROS)
    assert [t["chave"] for t in form["tabelas"]] == ["FGTS", "INSS", "IRRF"]
    fgts = form["tabelas"][0]
    assert fgts["valores"]["categoria_id"] == "cat-1" and fgts["valores"]["contato_id"] == "p-1"
    assert fgts["linhas_rateio"][0]["centro_id"] == "cc-2" and fgts["linhas_rateio"][0]["incluir"]
    assert form["tabelas"][2]["ignorada"] and form["competencia"] == "09/2026"


def test_folha_aprova_uma_tabela_por_vez_e_conclui(pastas, conta_azul_falsa):
    item_id = _criar_item(pastas, DADOS_FOLHA, nome="20260930_folha")

    r1 = revisao.aprovar_folha_tabela(item_id, "FGTS", _form_folha("500,00"), "Bruno", CADASTROS)
    assert r1["status"] == "lancado" and r1["documento_concluido"] is False
    salvo = json.loads((pastas / "revisao" / f"{item_id}_dados.json").read_text(encoding="utf-8"))
    assert salvo["dados"]["FGTS"]["sucesso"] is True and salvo["dados"]["FGTS"]["detalhes"]["aprovado_por"] == "Bruno"
    assert conta_azul_falsa["maturacao"] == [("fgts", "conferir o TRCT")]
    assert conta_azul_falsa["centros_funcionario"] == [("Energia Exemplo", "cc-2")]
    criado = conta_azul_falsa["criar"][0]
    assert criado["descricao"] == "FGTS 09/2026" and criado["data_competencia"] == "2026-09-01"
    assert criado["forma_pagamento"] == "boleto"

    assert revisao.aprovar_folha_tabela(item_id, "FGTS", _form_folha("500,00"), "Ana", CADASTROS) == {"status": "ja_tratado"}

    # A IRRF foi ignorada de propósito: lançada a INSS, o documento está resolvido.
    r2 = revisao.aprovar_folha_tabela(item_id, "INSS", _form_folha("800,00"), "Bruno", CADASTROS)
    assert r2["documento_concluido"] is True
    assert not (pastas / "revisao" / f"{item_id}_dados.json").exists()
    registros = list((pastas / "processados").glob("*_lancamento.json"))
    assert len(registros) == 2
    assert {json.loads(r.read_text(encoding="utf-8"))["aprovado_por"] for r in registros} == {"Bruno"}


def test_resumo_duplicado():
    resumo = revisao.resumo_duplicado({"registro_anterior": {
        "nome_arquivo_original": "a.pdf", "processado_em": "01/09/2026", "sucesso": True, "protocolo": "P9"}})
    assert resumo == {"nome_arquivo": "a.pdf", "processado_em": "01/09/2026", "sucesso": True, "protocolo": "P9", "motivo": None}
