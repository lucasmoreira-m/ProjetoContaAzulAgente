import pytest
from fastapi.testclient import TestClient

from src.web import app as web
from src.web import login_microsoft

MESMA_ORIGEM = {"origin": "http://testserver"}
TENANT = "tenant-exemplo"


class MsalFalso:
    """Simula a Microsoft: guarda o state gerado e devolve as claims pedidas."""

    claims = {"tid": TENANT, "preferred_username": "Bruno.Silva@empresa.com.br", "name": "Bruno Silva"}
    erro = None

    def initiate_auth_code_flow(self, escopos, redirect_uri):
        return {"auth_uri": f"https://login.microsoftonline.com/{TENANT}/authorize?state=abc", "state": "abc",
                "redirect_uri": redirect_uri}

    def acquire_token_by_auth_code_flow(self, fluxo, parametros):
        if parametros.get("state") != fluxo["state"]:
            raise ValueError("state mismatch")
        if self.erro:
            return {"error": self.erro, "error_description": "negado"}
        return {"access_token": "t", "id_token_claims": dict(self.claims)}


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    for nome in ("entrada", "processados", "revisao", "descartados"):
        (tmp_path / nome).mkdir()
        monkeypatch.setattr(web.config, f"PASTA_{nome.upper()}", str(tmp_path / nome))
    monkeypatch.setattr(web, "_ultimo_processamento", None)
    monkeypatch.setattr(web, "_em_andamento", None)
    monkeypatch.setattr(web.config, "LOGIN_MICROSOFT", True)
    monkeypatch.setattr(web.config, "EXIGIR_LOGIN_M365", True)  # o Dockerfile liga; o login do painel tem prioridade
    monkeypatch.setattr(web.config, "MS_TENANT_ID", TENANT)
    monkeypatch.setattr(web.config, "MS_REDIRECT_URI", "")
    monkeypatch.setattr(web.config, "USUARIOS_PERMITIDOS", set())
    falso = MsalFalso()
    monkeypatch.setattr(login_microsoft, "_app_msal", lambda: falso)
    c = TestClient(web.app)
    c.msal = falso
    return c


def _entrar(cliente, state="abc", voltar="/revisao"):
    inicio = cliente.get(f"/login/microsoft?voltar={voltar}", follow_redirects=False)
    assert inicio.status_code == 303 and inicio.headers["location"].startswith("https://login.microsoftonline.com/")
    return cliente.get(f"/auth/callback?code=xyz&state={state}", follow_redirects=False)


def test_sem_login_vai_para_a_tela_de_login(cliente):
    resposta = cliente.get("/revisao", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login?voltar=/revisao"
    tela = cliente.get("/login").text
    assert "Entrar com Microsoft" in tela
    assert cliente.post("/entrada/processar", headers=MESMA_ORIGEM).status_code == 401
    assert cliente.get("/saude").status_code == 200
    assert cliente.get("/static/estilo.css").status_code == 200


def test_login_completo_volta_para_a_pagina_pedida(cliente):
    retorno = _entrar(cliente)
    assert retorno.status_code == 303 and retorno.headers["location"] == "/revisao"
    pagina = cliente.get("/entrada").text
    assert "bruno.silva@empresa.com.br" in pagina and 'href="/sair"' in pagina
    assert 'name="nome"' not in pagina  # nome vem do login


def test_cabecalho_do_easy_auth_nao_vale_com_login_do_painel(cliente):
    resposta = cliente.get("/entrada", headers={web.CABECALHO_USUARIO_AZURE: "intruso@empresa.com.br"}, follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"].startswith("/login")


def test_state_diferente_e_recusado(cliente):
    retorno = _entrar(cliente, state="forjado")
    assert retorno.status_code == 400
    assert cliente.get("/entrada", follow_redirects=False).status_code == 303


def test_retorno_sem_ter_iniciado_login_e_recusado(cliente):
    assert cliente.get("/auth/callback?code=xyz&state=abc").status_code == 400


def test_conta_de_outro_tenant_e_recusada(cliente):
    cliente.msal.claims = dict(MsalFalso.claims, tid="outra-empresa")
    retorno = _entrar(cliente)
    assert retorno.status_code == 403 and "conta Microsoft 365 da empresa" in retorno.text


def test_lista_de_usuarios_permitidos(cliente, monkeypatch):
    monkeypatch.setattr(web.config, "USUARIOS_PERMITIDOS", {"ana@empresa.com.br"})
    assert _entrar(cliente).status_code == 403
    monkeypatch.setattr(web.config, "USUARIOS_PERMITIDOS", {"bruno.silva@empresa.com.br"})
    assert _entrar(cliente).status_code == 303


def test_microsoft_negou(cliente):
    cliente.msal.erro = "access_denied"
    assert _entrar(cliente).status_code == 401


def test_destino_externo_depois_do_login_e_ignorado(cliente):
    retorno = _entrar(cliente, voltar="//site-malicioso.com")
    assert retorno.headers["location"] == "/entrada"


def test_sair_encerra_a_sessao(cliente):
    _entrar(cliente)
    assert cliente.get("/entrada", follow_redirects=False).status_code == 200
    cliente.get("/sair")
    assert cliente.get("/entrada", follow_redirects=False).status_code == 303


def test_aprovacao_registra_o_email_do_login(cliente, monkeypatch):
    from tests.test_revisao import CADASTROS, DADOS_VIVO, FORM_VIVO, _criar_item
    import pathlib
    raiz = pathlib.Path(web.config.PASTA_REVISAO).parent
    item = _criar_item(raiz, DADOS_VIVO)
    monkeypatch.setattr(web.revisao, "carregar_cadastros", lambda forcar=False: CADASTROS)
    recebido = {}
    monkeypatch.setattr(web.revisao, "aprovar_comum",
                        lambda item_id, form, quem, cad, forcar: recebido.update(quem=quem) or {"status": "lancado"})
    _entrar(cliente)
    cliente.post(f"/revisao/{item}/aprovar", headers=MESMA_ORIGEM, data=dict(FORM_VIVO, aprovado_por="outro nome"))
    assert recebido["quem"] == "bruno.silva@empresa.com.br"
