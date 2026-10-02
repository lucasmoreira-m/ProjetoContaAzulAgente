"""
Login Microsoft 365 dentro do próprio painel (2026-10-01) - para rodar fora
do Azure (Google Cloud Run, PC), onde não existe o Easy Auth. Mesmo modelo
de login MSAL já usado em outro sistema interno (fluxo de código de
autorização), com duas diferenças de segurança:
- usa initiate_auth_code_flow do MSAL, que gera `state`, `nonce` e PKCE e os
  confere no retorno - sem isso alguém poderia fazer outra pessoa entrar
  logada com a conta dele (login CSRF);
- erros técnicos só vão para o log, nunca para a tela.

Liga sozinho quando MS_CLIENT_ID, MS_TENANT_ID e MS_CLIENT_SECRET estão
configurados. Só entram contas do tenant da empresa e, se USUARIOS_PERMITIDOS
estiver preenchida, só os e-mails da lista.
"""

from __future__ import annotations

import time

import msal
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from src.core import config

logger = config.get_logger(__name__)

roteador = APIRouter()

ESCOPOS = ["User.Read"]
DURACAO_SESSAO_SEGUNDOS = 8 * 60 * 60
# Rotas que não pedem login (a própria tela de login, o retorno da
# Microsoft, a sonda de saúde e os arquivos de estilo/imagem).
ROTAS_LIVRES = ("/login", "/auth/callback", "/saude", "/static/")

_templates = None  # injetado por src/web/app.py (mesmo Jinja do painel)


def configurar(templates) -> None:
    global _templates
    _templates = templates


def _app_msal() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=config.MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{config.MS_TENANT_ID}",
        client_credential=config.MS_CLIENT_SECRET,
    )


def _endereco_retorno(request: Request) -> str:
    """MS_REDIRECT_URI se configurado (precisa ser idêntico ao cadastrado no
    registro do app); senão, o próprio endereço do painel + /auth/callback."""
    return config.MS_REDIRECT_URI or str(request.url_for("retorno_login"))


def _destino_seguro(destino: str | None) -> str:
    """Só caminhos internos (evita redirecionar para outro site depois do login)."""
    if destino and destino.startswith("/") and not destino.startswith("//") and "\\" not in destino:
        return destino
    return "/entrada"


def usuario_da_sessao(request: Request) -> str | None:
    sessao = request.session
    if sessao.get("usuario") and time.time() < sessao.get("expira_em", 0):
        return sessao["usuario"]
    return None


def rota_livre(caminho: str) -> bool:
    return caminho.startswith(ROTAS_LIVRES)


def _tela_login(request: Request, erro: str | None = None, status_code: int = 200):
    return _templates.TemplateResponse(
        request, "login.html",
        {"erro": erro, "voltar": _destino_seguro(request.query_params.get("voltar")),
         "ambiente": config.conta_azul_ambiente()},
        status_code=status_code,
    )


@roteador.get("/login")
def tela_login(request: Request):
    if usuario_da_sessao(request):
        return RedirectResponse(_destino_seguro(request.query_params.get("voltar")), status_code=303)
    return _tela_login(request)


@roteador.get("/login/microsoft")
def iniciar_login(request: Request):
    fluxo = _app_msal().initiate_auth_code_flow(ESCOPOS, redirect_uri=_endereco_retorno(request))
    request.session["fluxo_login"] = fluxo
    request.session["voltar"] = _destino_seguro(request.query_params.get("voltar"))
    return RedirectResponse(fluxo["auth_uri"], status_code=303)


@roteador.get("/auth/callback", name="retorno_login")
def retorno_login(request: Request):
    fluxo = request.session.pop("fluxo_login", None)
    voltar = request.session.pop("voltar", "/entrada")
    if not fluxo:
        return _tela_login(request, "A tentativa de login expirou. Clique em Entrar de novo.", 400)
    try:
        resultado = _app_msal().acquire_token_by_auth_code_flow(fluxo, dict(request.query_params))
    except ValueError:
        # state diferente do esperado: resposta forjada ou de outra aba.
        logger.warning("Retorno de login recusado: state/nonce não confere.")
        return _tela_login(request, "Não foi possível confirmar o login. Clique em Entrar de novo.", 400)

    if "error" in resultado:
        logger.warning("Login Microsoft recusado: %s - %s", resultado.get("error"), resultado.get("error_description"))
        return _tela_login(request, "A Microsoft não confirmou o login. Tente de novo.", 401)

    claims = resultado.get("id_token_claims") or {}
    email = (claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    if claims.get("tid") != config.MS_TENANT_ID or not email:
        logger.warning("Login recusado: conta fora do tenant da empresa (%s).", email or "sem e-mail")
        return _tela_login(request, "Use a sua conta Microsoft 365 da empresa.", 403)
    if config.USUARIOS_PERMITIDOS and email not in config.USUARIOS_PERMITIDOS:
        logger.warning("Login recusado: %s não está em USUARIOS_PERMITIDOS.", email)
        return _tela_login(request, "Sua conta não tem acesso a este painel. Peça liberação ao responsável.", 403)

    request.session.clear()
    request.session.update(usuario=email, nome=claims.get("name") or email, expira_em=time.time() + DURACAO_SESSAO_SEGUNDOS)
    logger.info("Login de %s no painel.", email)
    return RedirectResponse(voltar, status_code=303)


@roteador.get("/sair")
def sair(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
