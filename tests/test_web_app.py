import json
import os

import pytest
from fastapi.testclient import TestClient

from src.web import app as web

MESMA_ORIGEM = {"origin": "http://testserver"}


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    for nome in ("entrada", "processados", "revisao", "descartados"):
        (tmp_path / nome).mkdir()
        monkeypatch.setattr(web.config, f"PASTA_{nome.upper()}", str(tmp_path / nome))
    monkeypatch.setattr(web, "_ultimo_processamento", None)
    monkeypatch.setattr(web, "_em_andamento", None)
    monkeypatch.setattr(web.config, "EXIGIR_LOGIN_M365", False)
    monkeypatch.setattr(web.config, "LOGIN_MICROSOFT", False)
    return tmp_path


@pytest.fixture
def cliente(pastas):
    # Sem "with": não roda o ciclo de vida (validar_configuracao exige .env).
    return TestClient(web.app)


def test_paginas_abrem(cliente):
    for pagina in ("/entrada", "/revisao", "/processados"):
        resposta = cliente.get(pagina)
        assert resposta.status_code == 200, pagina
        assert "Agente Conta Azul" in resposta.text


def test_upload_vai_para_pasta_de_quem_enviou_e_lembra_o_nome(cliente, pastas):
    resposta = cliente.post(
        "/entrada/enviar", headers=MESMA_ORIGEM, data={"nome": "Ana"},
        files=[("arquivos", ("fatura.pdf", b"%PDF-1.4", "application/pdf"))],
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert (pastas / "entrada" / "Ana" / "fatura.pdf").read_bytes() == b"%PDF-1.4"
    assert resposta.cookies.get(web.COOKIE_NOME) == "Ana"
    assert "fatura.pdf" in cliente.get("/entrada").text


def test_upload_usa_login_do_azure_e_ignora_nome_digitado(cliente, pastas, monkeypatch):
    monkeypatch.setattr(web.config, "EXIGIR_LOGIN_M365", True)
    cliente.post(
        "/entrada/enviar", headers={**MESMA_ORIGEM, web.CABECALHO_USUARIO_AZURE: "bruno.silva@empresa.com.br"},
        data={"nome": "outra pessoa"}, files=[("arquivos", ("a.pdf", b"%PDF", "application/pdf"))],
    )
    assert (pastas / "entrada" / "bruno.silva" / "a.pdf").exists()


def test_upload_recusa_tipo_nao_aceito(cliente, pastas):
    resposta = cliente.post(
        "/entrada/enviar", headers=MESMA_ORIGEM, data={"nome": "Ana"},
        files=[("arquivos", ("planilha.xlsx", b"x", "application/octet-stream"))], follow_redirects=False,
    )
    assert resposta.headers["location"].endswith("msg=tipo_invalido")
    assert not any(pastas.joinpath("entrada").rglob("*.xlsx"))


def test_post_de_outro_site_e_recusado(cliente, monkeypatch):
    chamado = []
    monkeypatch.setattr(web.processador, "processar_entrada", lambda **k: chamado.append(1) or [])

    resposta = cliente.post("/entrada/processar", headers={"origin": "https://site-malicioso.com"})
    assert resposta.status_code == 403
    assert cliente.post("/entrada/processar").status_code == 403  # sem Origin/Referer
    assert chamado == []


def test_processar_roda_em_segundo_plano_e_mostra_resultado(cliente, monkeypatch):
    itens = [{"arquivo": "a.pdf", "usuario": "Ana", "sucesso": True, "motivo": "lançado", "destino": "processados"}]
    monkeypatch.setattr(web.processador, "processar_entrada", lambda acionado_por: itens)

    resposta = cliente.post("/entrada/processar", headers=MESMA_ORIGEM, follow_redirects=False)
    assert resposta.headers["location"].endswith("msg=iniciado")
    web.aguardar_processamento()

    pagina = cliente.get("/entrada").text
    assert "a.pdf" in pagina and "1 lançado(s)" in pagina
    assert web._em_andamento is None


def test_enquanto_processa_pagina_mostra_andamento_e_recusa_segundo_lote(cliente, monkeypatch):
    import threading
    liberar = threading.Event()
    monkeypatch.setattr(web.processador, "processar_entrada", lambda acionado_por: liberar.wait(5) and [])
    try:
        cliente.post("/entrada/processar", headers=MESMA_ORIGEM)
        pagina = cliente.get("/entrada").text
        assert 'http-equiv="refresh"' in pagina and "Processando" in pagina
        segundo = cliente.post("/entrada/processar", headers=MESMA_ORIGEM, follow_redirects=False)
        assert segundo.headers["location"].endswith("msg=em_andamento")
    finally:
        liberar.set()
        web.aguardar_processamento()
    assert 'http-equiv="refresh"' not in cliente.get("/entrada").text


def test_erro_no_lote_aparece_na_pagina(cliente, monkeypatch):
    def _ocupado(acionado_por):
        raise web.processador.ProcessamentoEmAndamento()

    monkeypatch.setattr(web.processador, "processar_entrada", _ocupado)
    cliente.post("/entrada/processar", headers=MESMA_ORIGEM)
    web.aguardar_processamento()
    assert "Outro processamento" in cliente.get("/entrada").text


def test_no_azure_toda_pagina_exige_login_m365(cliente, monkeypatch):
    monkeypatch.setattr(web.config, "EXIGIR_LOGIN_M365", True)
    assert cliente.get("/entrada").status_code == 401
    cliente.cookies.set(web.COOKIE_NOME, "Ana")
    assert cliente.get("/revisao").status_code == 401  # cookie não vale
    assert cliente.get("/saude").status_code == 200  # sonda de saúde do Azure
    logado = cliente.get("/entrada", headers={web.CABECALHO_USUARIO_AZURE: "bruno.silva@empresa.com.br"})
    assert logado.status_code == 200 and "bruno.silva@empresa.com.br" in logado.text
    assert 'name="nome"' not in logado.text  # sem campo de nome: vale o login


def test_csrf_aceita_host_repassado_pelo_proxy(cliente, monkeypatch):
    monkeypatch.setattr(web.processador, "processar_entrada", lambda acionado_por: [])
    resposta = cliente.post("/entrada/processar", follow_redirects=False, headers={
        "origin": "https://agente.azurecontainerapps.io", "x-forwarded-host": "agente.azurecontainerapps.io"})
    assert resposta.status_code == 303
    web.aguardar_processamento()


def test_revisao_lista_pendencia_e_abre_o_pdf(cliente, pastas):
    (pastas / "revisao" / "20260930_fatura.pdf").write_bytes(b"%PDF")
    (pastas / "revisao" / "20260930_fatura_dados.json").write_text(json.dumps({
        "documento_original": "20260930_fatura.pdf", "enviado_por": "Ana", "processado_em": "30/09/2026 10:00:00",
        "sucesso": False, "motivo": "Contato não encontrado", "dados": {"fornecedor": "Telecom Exemplo", "valor_total": 123.4},
    }), encoding="utf-8")

    pagina = cliente.get("/revisao").text
    assert "Telecom Exemplo" in pagina and "R$ 123,40" in pagina and "Contato não encontrado" in pagina

    assert cliente.get("/documento/revisao/20260930_fatura.pdf").content == b"%PDF"


@pytest.mark.parametrize("caminho", [
    "/documento/revisao/..%2F..%2F.env",
    "/documento/descartados/x.pdf",
    "/documento/revisao/inexistente.pdf",
    "/documento/revisao/20260930_fatura_dados.json",
])
def test_documento_so_serve_arquivo_das_pastas_permitidas(cliente, pastas, caminho):
    (pastas / "revisao" / "20260930_fatura_dados.json").write_text("{}", encoding="utf-8")
    assert cliente.get(caminho).status_code == 404


@pytest.mark.parametrize("valor, esperado", [
    (1234.5, "R$ 1.234,50"), (0, "R$ 0,00"), (1234567.89, "R$ 1.234.567,89"), (None, "—"), ("abc", "—"),
])
def test_valor_brl(valor, esperado):
    assert web.valor_brl(valor) == esperado


# ---------------------------------------------------------------------------
# Revisão de documento comum
# ---------------------------------------------------------------------------
from tests.test_revisao import CADASTROS, DADOS_VIVO, FORM_VIVO, _criar_item  # noqa: E402


@pytest.fixture
def item_vivo(pastas, monkeypatch):
    monkeypatch.setattr(web.revisao.config, "PASTA_REVISAO", str(pastas / "revisao"))
    monkeypatch.setattr(web.revisao, "carregar_cadastros", lambda forcar=False: CADASTROS)
    monkeypatch.setattr(web.revisao.historico_centros_funcionarios, "buscar_diretrizes_globais_texto", lambda: {})
    return _criar_item(pastas, DADOS_VIVO)


def test_lista_da_revisao_tem_botao_revisar_para_documento_comum(cliente, item_vivo):
    pagina = cliente.get("/revisao").text
    assert f'href="/revisao/{item_vivo}"' in pagina
    assert "Documento comum" in pagina


def test_tela_do_item_mostra_formulario_com_sugestoes(cliente, item_vivo):
    pagina = cliente.get(f"/revisao/{item_vivo}").text
    assert 'action="/revisao/%s/aprovar"' % item_vivo in pagina
    assert '<option value="cat-2" selected>' in pagina
    assert '<option value="p-2" selected>' in pagina
    assert 'value="300,00"' in pagina
    assert "Diretoria" in pagina  # linha do rateio


def test_aprovar_redireciona_com_protocolo(cliente, item_vivo, monkeypatch):
    recebido = {}

    def _aprovar(item_id, form, quem, cadastros, forcar):
        recebido.update(item_id=item_id, quem=quem, forcar=forcar, valor=form["valor"])
        return {"status": "lancado", "protocolo": "PROT-1", "reextraidos": 0}

    monkeypatch.setattr(web.revisao, "aprovar_comum", _aprovar)
    resposta = cliente.post(f"/revisao/{item_vivo}/aprovar", headers=MESMA_ORIGEM,
                            data=dict(FORM_VIVO, aprovado_por="Bruno"), follow_redirects=False)

    assert resposta.status_code == 303
    assert "protocolo=PROT-1" in resposta.headers["location"]
    assert recebido == {"item_id": item_vivo, "quem": "Bruno", "forcar": False, "valor": "300,00"}
    assert "Protocolo: PROT-1" in cliente.get(resposta.headers["location"]).text


def test_aprovar_sem_nome_pede_o_nome(cliente, item_vivo, monkeypatch):
    monkeypatch.setattr(web.revisao, "aprovar_comum", lambda *a: pytest.fail("não devia lançar"))
    resposta = cliente.post(f"/revisao/{item_vivo}/aprovar", headers=MESMA_ORIGEM, data=FORM_VIVO)
    assert resposta.status_code == 400
    assert "Informe seu nome" in resposta.text


def test_duplicata_mostra_aviso_e_botao_de_confirmacao(cliente, item_vivo, monkeypatch):
    monkeypatch.setattr(web.revisao, "aprovar_comum",
                        lambda *a: {"status": "duplicata", "duplicata": {"id": "x-9", "status": "PAGO"}})
    resposta = cliente.post(f"/revisao/{item_vivo}/aprovar", headers=MESMA_ORIGEM,
                            data=dict(FORM_VIVO, aprovado_por="Bruno", valor="299,90"))
    assert resposta.status_code == 200
    assert "Possível duplicata" in resposta.text and "x-9" in resposta.text
    assert 'name="forcar" value="1"' in resposta.text
    assert 'value="299,90"' in resposta.text  # mantém o que foi digitado


def test_confirmar_duplicata_envia_forcar(cliente, item_vivo, monkeypatch):
    recebido = {}
    monkeypatch.setattr(web.revisao, "aprovar_comum",
                        lambda item_id, form, quem, cad, forcar: recebido.update(forcar=forcar) or {"status": "lancado"})
    cliente.post(f"/revisao/{item_vivo}/aprovar", headers=MESMA_ORIGEM,
                 data=dict(FORM_VIVO, aprovado_por="Bruno", forcar="1"), follow_redirects=False)
    assert recebido["forcar"] is True


def test_item_em_uso_avisa(cliente, item_vivo, monkeypatch):
    def _ocupado(*a):
        raise web.revisao.ItemEmUso()

    monkeypatch.setattr(web.revisao, "aprovar_comum", _ocupado)
    resposta = cliente.post(f"/revisao/{item_vivo}/aprovar", headers=MESMA_ORIGEM, data=dict(FORM_VIVO, aprovado_por="Bruno"))
    assert resposta.status_code == 409
    assert "Outra pessoa" in resposta.text


def test_telas_de_nfse_folha_e_duplicata_abrem_com_aprovar_e_descartar(cliente, pastas, item_vivo, monkeypatch):
    from tests.test_revisao import CADASTROS_NFSE, DADOS_FOLHA, DADOS_NFSE
    monkeypatch.setattr(web.revisao, "carregar_cadastros", lambda forcar=False: CADASTROS_NFSE)
    _criar_item(pastas, DADOS_NFSE, nome="20260930_nfse")
    _criar_item(pastas, DADOS_FOLHA, nome="20260930_folha")
    _criar_item(pastas, {"registro_anterior": {"nome_arquivo_original": "velho.pdf", "sucesso": True, "protocolo": "P9"}},
                nome="20260930_dup")

    nfse = cliente.get("/revisao/20260930_nfse").text
    assert 'action="/revisao/20260930_nfse/aprovar"' in nfse and 'name="banco"' in nfse
    assert 'name="descricao"' in nfse and 'name="valor_liquido"' in nfse and 'name="orientacao"' in nfse
    assert 'action="/revisao/20260930_nfse/descartar"' in nfse

    folha = cliente.get("/revisao/20260930_folha").text
    for tabela in ("FGTS", "INSS", "IRRF"):
        assert f'action="/revisao/20260930_folha/folha/{tabela}/aprovar"' in folha
    assert "Ignorado / revisão" in folha and 'action="/revisao/20260930_folha/descartar"' in folha

    dup = cliente.get("/revisao/20260930_dup").text
    assert "velho.pdf" in dup and "P9" in dup
    assert 'action="/revisao/20260930_dup/descartar"' in dup and "/aprovar" not in dup

    lista = cliente.get("/revisao").text
    for item in (item_vivo, "20260930_nfse", "20260930_folha", "20260930_dup"):
        assert f'href="/revisao/{item}"' in lista and f'action="/revisao/{item}/descartar"' in lista


def test_aprovar_tabela_da_folha_volta_para_o_documento(cliente, pastas, item_vivo, monkeypatch):
    from tests.test_revisao import DADOS_FOLHA
    _criar_item(pastas, DADOS_FOLHA, nome="20260930_folha")
    recebido = {}

    def _aprovar(item_id, tabela, form, quem, cadastros, forcar):
        recebido.update(item_id=item_id, tabela=tabela, quem=quem)
        return {"status": "lancado", "protocolo": "P1", "documento_concluido": False}

    monkeypatch.setattr(web.revisao, "aprovar_folha_tabela", _aprovar)
    resposta = cliente.post("/revisao/20260930_folha/folha/FGTS/aprovar", headers=MESMA_ORIGEM,
                            data={"aprovado_por": "Bruno", "valor": "500,00"}, follow_redirects=False)
    assert resposta.headers["location"].startswith("/revisao/20260930_folha?msg=lancado")
    assert recebido == {"item_id": "20260930_folha", "tabela": "FGTS", "quem": "Bruno"}


def test_aprovar_duplicata_de_arquivo_nao_existe(cliente, pastas, item_vivo):
    _criar_item(pastas, {"registro_anterior": {}}, nome="20260930_dup")
    resposta = cliente.post("/revisao/20260930_dup/aprovar", headers=MESMA_ORIGEM, data={"aprovado_por": "Bruno"},
                            follow_redirects=False)
    assert resposta.headers["location"].endswith("msg=acao_invalida")


def test_descartar_pela_pagina(cliente, pastas, item_vivo):
    resposta = cliente.post(f"/revisao/{item_vivo}/descartar", headers=MESMA_ORIGEM, data={"aprovado_por": "Bruno"},
                            follow_redirects=False)
    assert resposta.headers["location"].endswith("msg=descartado")
    assert not (pastas / "revisao" / f"{item_vivo}_dados.json").exists()


def test_fora_do_azure_cabecalho_de_login_nao_vale(cliente, pastas):
    """Sem Easy Auth na frente, qualquer um poderia mandar o cabeçalho."""
    pagina = cliente.get("/entrada", headers={web.CABECALHO_USUARIO_AZURE: "intruso@empresa.com.br"}).text
    assert "intruso@empresa.com.br" not in pagina
