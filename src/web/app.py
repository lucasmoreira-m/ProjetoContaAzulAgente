"""
Painel web do Agente Conta Azul em FastAPI (2026-09-30) - substitui o
Streamlit, que roda a página inteira de novo a cada clique e deu muitos
problemas de estado. Aqui cada botão é um POST para uma rota, e a rota
redireciona de volta para a página (POST-redirect-GET: dar F5 não repete
a ação).

Subir, na raiz do projeto:
    python -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000
(ou atalhos/abrir_painel.bat)

Revisão de todos os tipos (documento comum, NFS-e, folha e duplicata de
arquivo) com aprovar e descartar - o painel Streamlit foi aposentado.
"""

from __future__ import annotations

import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from src.core import config
from src.core.conta_azul_client import ContaAzulAPIError
from src.agentes import monitor_pasta
from src.agentes import processador
from src.agentes import revisao
from src.web import login_microsoft
from src.utils import historico_lancamentos
from src.utils import pastas_trabalho
from src.utils.formato_texto import data_para_texto_br

logger = config.get_logger(__name__)

PASTA_WEB = Path(__file__).resolve().parent
TAMANHO_MAXIMO_UPLOAD = 25 * 1024 * 1024

# Cabeçalho com o e-mail de quem logou, injetado pelo Easy Auth do Azure
# (login M365). O Easy Auth descarta esse cabeçalho se vier do navegador,
# então lá ele é confiável; rodando local ninguém injeta e vale o nome
# digitado (cookie). Não publicar sem o Easy Auth na frente.
CABECALHO_USUARIO_AZURE = "x-ms-client-principal-name"
COOKIE_NOME = "nome_usuario"

MENSAGENS = {
    "enviados": ("ok", "Arquivo(s) enviado(s) para a entrada."),
    "nada_enviado": ("aviso", "Nenhum arquivo selecionado."),
    "tipo_invalido": ("erro", "Tipo de arquivo não aceito. Envie PDF, JPG ou PNG."),
    "grande_demais": ("erro", "Arquivo acima de 25 MB."),
    "em_andamento": ("aviso", "Já existe um processamento em andamento. Aguarde terminar e atualize a página."),
    "processado": ("ok", "Processamento concluído."),
    "iniciado": ("ok", "Processamento iniciado. Esta página se atualiza sozinha até terminar."),
    "lancado": ("ok", "Lançado na Conta Azul."),
    "descartado": ("ok", "Item descartado."),
    "ja_tratado": ("aviso", "Este item já não está mais na revisão (outra pessoa pode ter tratado)."),
    "item_em_uso": ("aviso", "Outra pessoa está tratando este item agora. Tente de novo em instantes."),
    "acao_invalida": ("erro", "Ação não disponível para este tipo de documento."),
}


@asynccontextmanager
async def _ciclo_de_vida(app: FastAPI):
    config.validar_configuracao()
    for pasta in (config.PASTA_ENTRADA, config.PASTA_PROCESSADOS, config.PASTA_REVISAO, config.PASTA_DESCARTADOS):
        os.makedirs(pasta, exist_ok=True)
    yield


app = FastAPI(title="Agente Conta Azul", lifespan=_ciclo_de_vida, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=PASTA_WEB / "static"), name="static")
templates = Jinja2Templates(directory=PASTA_WEB / "templates")


def valor_brl(valor) -> str:
    """1234.5 -> 'R$ 1.234,50'. Vazio/inválido -> '—'."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return "—"
    return "R$ " + f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters["brl"] = valor_brl
templates.env.filters["data_br"] = lambda valor: data_para_texto_br(valor, padrao="—")
login_microsoft.configurar(templates)
app.include_router(login_microsoft.roteador)

# Processamento em segundo plano (2026-10-01): a entrada do Azure Container
# Apps corta requisições depois de ~4 minutos, e um lote grande de PDFs com
# o Gemini passa disso. O botão só dispara o lote numa thread e volta na
# hora; a página mostra o andamento e se atualiza sozinha. O resultado do
# último lote fica visível para todos (a fila é única).
_trava_ultimo = threading.Lock()
_ultimo_processamento: dict | None = None
_em_andamento: dict | None = None  # {"desde", "por", "qtd"} enquanto roda
_thread_processamento: threading.Thread | None = None


@app.middleware("http")
async def _bloquear_post_de_outro_site(request: Request, call_next):
    """Proteção CSRF: um POST só é aceito se o navegador disser que veio
    desta mesma página (Origin/Referer com o mesmo host). Sem isso, um site
    qualquer aberto no mesmo navegador poderia disparar "Processar". Atrás
    do proxy do Azure o host original também pode vir em X-Forwarded-Host."""
    if request.method == "POST":
        origem = request.headers.get("origin") or request.headers.get("referer")
        hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")} - {None}
        if not origem or urlparse(origem).netloc not in hosts:
            return JSONResponse({"erro": "Requisição de outra origem recusada."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _exigir_login_microsoft(request: Request, call_next):
    """Com o login Microsoft do painel ligado, quem não entrou vai para a tela
    de login (ou recebe 401, se for um envio de formulário)."""
    if config.LOGIN_MICROSOFT and not login_microsoft.rota_livre(request.url.path) \
            and not login_microsoft.usuario_da_sessao(request):
        if request.method == "GET":
            return RedirectResponse(f"/login?voltar={quote(request.url.path)}", status_code=303)
        return JSONResponse({"erro": "Sessão expirada. Entre de novo."}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def _exigir_login_m365(request: Request, call_next):
    """No Azure (EXIGIR_LOGIN_M365=1, ligado no Dockerfile) toda página exige
    o login Microsoft 365 do Easy Auth - se alguém esquecer de ligar a
    autenticação no Container App, o painel recusa em vez de ficar aberto na
    internet. /saude fica livre para a sonda de saúde do Azure."""
    if config.EXIGIR_LOGIN_M365 and not config.LOGIN_MICROSOFT and request.url.path != "/saude" and not request.headers.get(CABECALHO_USUARIO_AZURE):
        return HTMLResponse(
            "<h1>Acesso restrito</h1><p>Entre com a sua conta Microsoft 365 da empresa. Se você já entrou e vê esta "
            "mensagem, a autenticação do Azure não está ligada neste aplicativo: avise o responsável.</p>",
            status_code=401,
        )
    return await call_next(request)


# Cookie de sessão assinado (login Microsoft). Adicionado DEPOIS dos
# middlewares acima, para ser o mais externo e request.session existir neles.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSAO_CHAVE or secrets.token_urlsafe(32),
    session_cookie="sessao_painel",
    max_age=login_microsoft.DURACAO_SESSAO_SEGUNDOS,
    same_site="lax",
    https_only=False,  # localhost; o Cloud Run/Azure já serve só HTTPS
)


def usuario_atual(request: Request) -> tuple[str | None, bool]:
    """(nome, veio_do_login), conforme o modo de login:
    - login Microsoft do próprio painel (Google Cloud Run, PC): a sessão;
    - Azure com Easy Auth: o cabeçalho que o Azure injeta. Só nesse modo o
      cabeçalho é confiável - fora do Azure qualquer um poderia mandá-lo;
    - sem login (PC): o nome digitado, lembrado num cookie."""
    if config.LOGIN_MICROSOFT:
        return login_microsoft.usuario_da_sessao(request), True
    if config.EXIGIR_LOGIN_M365:
        return request.headers.get(CABECALHO_USUARIO_AZURE) or None, True
    return request.cookies.get(COOKIE_NOME) or None, False


def _contexto(request: Request, aba: str, **extra) -> dict:
    nome, logado = usuario_atual(request)
    codigo = request.query_params.get("msg")
    mensagem = MENSAGENS.get(codigo)
    if codigo == "lancado" and mensagem:
        texto = mensagem[1]
        if request.query_params.get("protocolo"):
            texto += f" Protocolo: {request.query_params['protocolo']}."
        if request.query_params.get("reextraidos", "0").isdigit() and int(request.query_params.get("reextraidos", "0")):
            texto += f" Orientação também aplicada a {request.query_params['reextraidos']} documento(s) que aguardavam revisão."
        mensagem = (mensagem[0], texto)
    return {
        "aba": aba,
        "usuario": nome,
        "usuario_logado": logado,
        "login_microsoft": config.LOGIN_MICROSOFT,
        "ambiente": config.conta_azul_ambiente(),
        "mensagem": mensagem,
        "qtd_revisao": len(pastas_trabalho.listar_pendencias()),
        **extra,
    }


def _voltar(pagina: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{pagina}?msg={msg}", status_code=303)


@app.get("/")
def inicio():
    return RedirectResponse("/entrada", status_code=303)


@app.get("/saude")
def saude():
    return {"ok": True, "ambiente": config.conta_azul_ambiente()}


@app.get("/entrada")
def pagina_entrada(request: Request):
    arquivos = []
    for caminho, dono in monitor_pasta.listar_arquivos_com_usuario():
        arquivos.append({
            "nome": os.path.basename(caminho),
            "dono": dono,
            "enviado_em": datetime.fromtimestamp(os.path.getmtime(caminho)).strftime("%d/%m/%Y %H:%M"),
        })
    with _trava_ultimo:
        ultimo, andamento = _ultimo_processamento, _em_andamento
    return templates.TemplateResponse(
        request, "entrada.html",
        _contexto(request, "entrada", arquivos=arquivos, ultimo=ultimo, andamento=andamento,
                  qtd_processados=len(historico_lancamentos.listar_historico()),
                  extensoes=",".join(monitor_pasta.EXTENSOES_ACEITAS)),
    )


@app.post("/entrada/enviar")
def enviar_arquivos(request: Request, arquivos: list[UploadFile], nome: str = Form("")):
    nome_login, logado = usuario_atual(request)
    quem = nome_login if logado else nome.strip()
    usuario = pastas_trabalho.nome_pasta_usuario(quem)

    validos = [a for a in arquivos if a.filename]
    if not validos:
        return _voltar("/entrada", "nada_enviado")
    for arquivo in validos:
        if not arquivo.filename.lower().endswith(monitor_pasta.EXTENSOES_ACEITAS):
            return _voltar("/entrada", "tipo_invalido")
        if arquivo.size is not None and arquivo.size > TAMANHO_MAXIMO_UPLOAD:
            return _voltar("/entrada", "grande_demais")

    for arquivo in validos:
        destino = pastas_trabalho.caminho_destino_upload(arquivo.filename, usuario)
        gravado = 0
        with open(destino, "wb") as f:
            while bloco := arquivo.file.read(1024 * 1024):
                gravado += len(bloco)
                if gravado > TAMANHO_MAXIMO_UPLOAD:
                    break
                f.write(bloco)
        if gravado > TAMANHO_MAXIMO_UPLOAD:
            os.remove(destino)
            return _voltar("/entrada", "grande_demais")
        logger.info("Arquivo %s enviado pela página por %s.", os.path.basename(destino), usuario)

    resposta = _voltar("/entrada", "enviados")
    if not logado and quem:
        resposta.set_cookie(COOKIE_NOME, quem, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resposta


def _rodar_lote(quem: str) -> None:
    """Thread do lote: processa o que está na entrada e guarda o resultado."""
    global _ultimo_processamento, _em_andamento
    resumo = {"quando": None, "por": quem, "itens": [], "lancados": 0, "erro": None}
    try:
        itens = processador.processar_entrada(acionado_por=quem)
        resumo.update(itens=itens, lancados=sum(1 for i in itens if i["sucesso"]))
    except processador.ProcessamentoEmAndamento:
        resumo["erro"] = "Outro processamento (em outro processo) estava rodando. Tente de novo em instantes."
    except Exception:
        logger.exception("Erro inesperado no processamento da entrada acionado por %s", quem)
        resumo["erro"] = "Erro inesperado no processamento. Veja os logs para detalhes."
    resumo["quando"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    with _trava_ultimo:
        _ultimo_processamento, _em_andamento = resumo, None


@app.post("/entrada/processar")
def processar(request: Request):
    """Processa o que está na entrada AGORA e para, numa thread - a página
    volta na hora e acompanha o andamento."""
    global _em_andamento, _thread_processamento
    if config.PROCESSADOR != "local":
        raise HTTPException(status_code=503, detail="Processamento no Azure ainda não disponível (PROCESSADOR=local).")
    nome, _ = usuario_atual(request)
    quem = nome or "sem nome"
    with _trava_ultimo:
        if _em_andamento is not None:
            return _voltar("/entrada", "em_andamento")
        _em_andamento = {
            "desde": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "por": quem,
            "qtd": len(monitor_pasta.listar_arquivos_com_usuario()),
        }
        _thread_processamento = threading.Thread(target=_rodar_lote, args=(quem,), name="processar-entrada", daemon=True)
        _thread_processamento.start()
    return _voltar("/entrada", "iniciado")


def aguardar_processamento(timeout: float = 30) -> None:
    """Espera o lote em segundo plano terminar (testes)."""
    thread = _thread_processamento
    if thread is not None:
        thread.join(timeout)


NOME_TIPO_PENDENCIA = {"comum": "Documento comum", "nfse": "NFS-e", "folha": "Folha de pagamento", "duplicado": "Arquivo duplicado"}
NOME_TIPO_CADASTRO = {
    "categoria": "Categorias", "contato": "Contatos/fornecedores",
    "centro_custo": "Centros de custo", "conta_financeira": "Contas financeiras",
}


def _cadastros_pendentes(pendencias: list[dict]) -> dict[str, list[dict]]:
    """Tudo que os documentos em revisão esperavam achar na Conta Azul e
    não acharam, juntando todos os itens, por tipo de cadastro."""
    vistos, por_tipo = set(), {}
    for p in pendencias:
        for c in p["info"].get("cadastros_pendentes", []):
            chave = (c["tipo"], (c["nome_no_documento"] or "").strip().lower())
            if chave not in vistos:
                vistos.add(chave)
                por_tipo.setdefault(NOME_TIPO_CADASTRO.get(c["tipo"], c["tipo"]), []).append(c)
    return por_tipo


@app.get("/revisao")
def pagina_revisao(request: Request):
    pendencias = pastas_trabalho.listar_pendencias()
    itens = []
    for p in pendencias:
        info = p["info"]
        dados = info.get("dados")
        tipo = revisao.tipo_pendencia(dados)
        if not isinstance(dados, dict):
            dados = {}
        itens.append({
            "id": revisao.id_do_item(p["caminho_json"]),
            "tipo": tipo,
            "tipo_legivel": NOME_TIPO_PENDENCIA[tipo],
            "documento": info.get("documento_original") or os.path.basename(p["caminho_json"]),
            "arquivo": os.path.basename(p["caminho_documento"]) if p["caminho_documento"] else None,
            "enviado_por": info.get("enviado_por"),
            "processado_em": info.get("processado_em"),
            "motivo": info.get("motivo"),
            # Comum: fornecedor; NFS-e: fornecedor_nome; folha: empresa.
            "fornecedor": dados.get("fornecedor") or dados.get("fornecedor_nome") or dados.get("empresa"),
            "valor": dados.get("valor_total"),
        })
    return templates.TemplateResponse(
        request, "revisao.html",
        _contexto(request, "revisao", itens=itens,
                  cadastros_pendentes=_cadastros_pendentes(pendencias)),
    )


TEMPLATE_POR_TIPO = {
    "comum": "revisao_comum.html",
    "nfse": "revisao_nfse.html",
    "folha": "revisao_folha.html",
    "duplicado": "revisao_duplicado.html",
}


def _pagina_item(request: Request, item: dict, enviado: dict | None = None, status_code: int = 200,
                 tabela_enviada: str | None = None, **extra):
    """Tela de revisão de um item, conforme o tipo. Carrega os cadastros da
    Conta Azul só aqui (listar pendências não gasta o refresh_token) e só
    para os tipos que lançam (duplicata de arquivo só descarta)."""
    info = item["info"]
    dados = info.get("dados")
    tipo = revisao.tipo_pendencia(dados)
    doc = item["caminho_documento"]
    contexto = {
        "item_id": revisao.id_do_item(item["caminho_json"]),
        "info": info,
        "tipo": tipo,
        "arquivo": os.path.basename(doc) if doc else None,
        "eh_imagem": bool(doc) and doc.lower().endswith((".jpg", ".jpeg", ".png")),
        "motivo": revisao.partes_do_motivo(info.get("motivo")),
    }
    if tipo == "duplicado":
        contexto["anterior"] = revisao.resumo_duplicado(dados)
    else:
        try:
            cadastros = revisao.carregar_cadastros()
        except ContaAzulAPIError as e:
            return templates.TemplateResponse(
                request, "revisao_erro.html",
                _contexto(request, "revisao", erro=f"Não foi possível carregar os cadastros da Conta Azul: {e}"),
                status_code=502,
            )
        if tipo == "comum":
            contexto["form"] = revisao.formulario_comum(dados, cadastros, enviado)
        elif tipo == "nfse":
            contexto["form"] = revisao.formulario_nfse(dados, cadastros, enviado)
        else:
            contexto["form"] = revisao.formulario_folha(dados, cadastros, tabela_enviada, enviado)
            contexto["tabela_enviada"] = tabela_enviada
    return templates.TemplateResponse(
        request, TEMPLATE_POR_TIPO[tipo], _contexto(request, "revisao", **contexto, **extra), status_code=status_code,
    )


@app.get("/revisao/{item_id}")
def pagina_item(request: Request, item_id: str):
    item = revisao.carregar_item(item_id)
    if item is None:
        return _voltar("/revisao", "ja_tratado")
    return _pagina_item(request, item)


def _quem_age(request: Request, form: dict) -> str | None:
    nome, logado = usuario_atual(request)
    if logado:
        return nome
    return (form.get("aprovado_por") or nome or "").strip() or None


def _lembrar_nome(request: Request, resposta, nome: str | None) -> None:
    if nome and not usuario_atual(request)[1]:
        resposta.set_cookie(COOKIE_NOME, nome, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")


async def _aprovar(request: Request, item_id: str, tabela: str | None = None):
    """Aprovação de qualquer tipo que lança: comum, NFS-e ou uma tabela da
    folha. Duplicata/erro reabrem a mesma tela com o que foi digitado."""
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    item = revisao.carregar_item(item_id)
    if item is None:
        return _voltar("/revisao", "ja_tratado")
    tipo = revisao.tipo_pendencia(item["info"].get("dados"))
    if tipo == "duplicado" or (tipo == "folha") != (tabela is not None):
        return _voltar(f"/revisao/{quote(item_id)}", "acao_invalida")

    def _reabrir(status_code=200, **extra):
        return _pagina_item(request, item, form, status_code, tabela_enviada=tabela, **extra)

    quem = _quem_age(request, form)
    if not quem:
        return _reabrir(400, erro="Informe seu nome para aprovar.")

    forcar = form.get("forcar") == "1"
    try:
        cadastros = await run_in_threadpool(revisao.carregar_cadastros)
        if tipo == "comum":
            resultado = await run_in_threadpool(revisao.aprovar_comum, item_id, form, quem, cadastros, forcar)
        elif tipo == "nfse":
            resultado = await run_in_threadpool(revisao.aprovar_nfse, item_id, form, quem, cadastros, forcar)
        else:
            resultado = await run_in_threadpool(revisao.aprovar_folha_tabela, item_id, tabela, form, quem, cadastros, forcar)
    except revisao.ItemEmUso:
        return _reabrir(409, erro="Outra pessoa está aprovando ou descartando este item agora.")
    except ContaAzulAPIError as e:
        return _reabrir(502, erro=f"Erro de comunicação com a Conta Azul: {e}")
    except Exception:
        logger.exception("Erro inesperado ao aprovar %s pelo painel web", item_id)
        return _reabrir(500, erro="Erro inesperado ao lançar. Veja os logs para detalhes.")

    if resultado["status"] == "lancado":
        # Folha com outras tabelas pendentes: volta para o mesmo documento.
        destino = "/revisao"
        if tipo == "folha" and not resultado.get("documento_concluido"):
            destino = f"/revisao/{quote(item_id)}"
        resposta = RedirectResponse(
            f"{destino}?msg=lancado&protocolo={quote(str(resultado.get('protocolo') or ''))}"
            f"&reextraidos={int(resultado.get('reextraidos') or 0)}", status_code=303,
        )
        _lembrar_nome(request, resposta, quem)
        return resposta
    if resultado["status"] == "ja_tratado":
        return _voltar("/revisao", "ja_tratado")
    if resultado["status"] == "duplicata":
        return _reabrir(duplicata=resultado["duplicata"])
    return _reabrir(400, erro=resultado.get("mensagem"))


@app.post("/revisao/{item_id}/aprovar")
async def aprovar_item(request: Request, item_id: str):
    return await _aprovar(request, item_id)


@app.post("/revisao/{item_id}/folha/{tabela}/aprovar")
async def aprovar_tabela_folha(request: Request, item_id: str, tabela: str):
    return await _aprovar(request, item_id, tabela)


@app.post("/revisao/{item_id}/descartar")
async def descartar_item(request: Request, item_id: str):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    quem = _quem_age(request, form) or "sem nome"
    try:
        descartado = await run_in_threadpool(revisao.descartar, item_id, quem)
    except revisao.ItemEmUso:
        return _voltar("/revisao", "item_em_uso")
    return _voltar("/revisao", "descartado" if descartado else "ja_tratado")


@app.post("/cadastros/atualizar")
async def atualizar_cadastros(request: Request):
    """Limpa o cache de 2 minutos - para usar logo depois de criar um
    contato/categoria/centro de custo na Conta Azul."""
    form = await request.form()
    revisao.limpar_cache_cadastros()
    voltar = str(form.get("voltar") or "/revisao")
    if not voltar.startswith("/revisao"):
        voltar = "/revisao"
    return RedirectResponse(voltar, status_code=303)


@app.get("/processados")
def pagina_processados(request: Request):
    return templates.TemplateResponse(
        request, "processados.html",
        _contexto(request, "processados", historico=historico_lancamentos.listar_historico()),
    )


@app.get("/documento/{pasta}/{nome}")
def abrir_documento(pasta: str, nome: str):
    """PDF/imagem de revisao/ ou processados/ para abrir no navegador. Só
    aceita nome de arquivo (sem caminho) dentro dessas duas pastas."""
    raiz = {"revisao": config.PASTA_REVISAO, "processados": config.PASTA_PROCESSADOS}.get(pasta)
    if raiz is None or nome != os.path.basename(nome) or not nome.lower().endswith(monitor_pasta.EXTENSOES_ACEITAS):
        raise HTTPException(status_code=404)
    caminho = os.path.join(raiz, nome)
    if not os.path.isfile(caminho):
        raise HTTPException(status_code=404)
    return FileResponse(caminho, content_disposition_type="inline")
