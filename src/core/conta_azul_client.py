"""
Cliente HTTP compartilhado para a API da Conta Azul.

Corrige, num único lugar (Escopo 2 e 5 da auditoria):
- Sessão reutilizável (requests.Session), em vez de abrir uma conexão nova
  a cada chamada.
- Timeout em TODAS as requisições (antes não havia nenhum -> risco de hang
  indefinido, especialmente ruim no loop de polling do monitor_pasta.py).
- Retry com backoff exponencial para erros de rede e para respostas 5xx/429
  (rate limit) - antes só o Gemini tinha retry.
- Retry SEPARADO para leitura e escrita (2026-09-24): GET repete em 429/5xx
  e timeout de leitura; POST/PUT/PATCH só repetem quando há GARANTIA de que
  a Conta Azul não gravou nada (falha de conexão ou 429). Antes o POST
  também repetia em 502/503/504 - um 504 pode chegar DEPOIS que o
  lançamento já foi criado, e o retry criava um lançamento duplicado.
- HTTPS obrigatório e verify=True (padrão do requests) - nunca desabilitado.
- Erros da API nunca vazam corpo bruto de resposta (que pode conter dados
  sensíveis) para logs; a mensagem de exceção é resumida e o corpo completo
  só vai para o log em nível DEBUG.
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.core import config

logger = config.get_logger(__name__)


class ContaAzulAPIError(Exception):
    """Erro ao chamar a API da Conta Azul. Mensagem já é segura para exibir
    ao usuário final (não contém token nem corpo bruto da resposta).

    `resultado_incerto=True` só em escrita (POST/PUT/PATCH) quando a
    requisição pode ter chegado à Conta Azul e sido gravada apesar do erro
    (timeout de leitura ou 5xx) - quem chamou NÃO deve reenviar sem antes
    conferir se o lançamento já existe (ver verificacao_duplicidade.py)."""

    def __init__(self, status_code: int | None, mensagem: str, resultado_incerto: bool = False):
        self.status_code = status_code
        self.resultado_incerto = resultado_incerto
        super().__init__(mensagem)


_METODOS_ESCRITA = ["POST", "PUT", "PATCH"]


def _construir_sessao(escrita: bool = False) -> requests.Session:
    sessao = requests.Session()
    if escrita:
        # Só repete o que COMPROVADAMENTE não foi processado: falha de
        # conexão (connect - a requisição nem saiu) e 429 (a Conta Azul
        # recusou antes de processar). read=0: timeout de leitura em escrita
        # nunca repete, porque o servidor pode ter gravado.
        retry = Retry(
            total=config.HTTP_MAX_RETRIES,
            connect=config.HTTP_MAX_RETRIES,
            read=0,
            status=config.HTTP_MAX_RETRIES,
            backoff_factor=config.HTTP_BACKOFF_FACTOR,
            status_forcelist=[429],
            allowed_methods=_METODOS_ESCRITA,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
    else:
        retry = Retry(
            total=config.HTTP_MAX_RETRIES,
            backoff_factor=config.HTTP_BACKOFF_FACTOR,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
    adapter = HTTPAdapter(max_retries=retry)
    sessao.mount("https://", adapter)
    # Nunca permitir HTTP puro para a API da Conta Azul.
    sessao.mount("http://", HTTPAdapter(max_retries=0))
    return sessao


_sessao = _construir_sessao()
_sessao_escrita = _construir_sessao(escrita=True)


def _url(caminho: str) -> str:
    if not caminho.startswith("/"):
        caminho = "/" + caminho
    url = f"{config.CONTA_AZUL_BASE_URL}{caminho}"
    if not url.startswith("https://"):
        # Defesa extra: nunca deixar uma chamada sair em HTTP puro.
        raise ContaAzulAPIError(None, "URL inválida: apenas HTTPS é permitido.")
    return url


def get(access_token: str, caminho: str, params: dict | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _sessao.get(
            _url(caminho), headers=headers, params=params, timeout=config.HTTP_TIMEOUT_SEGUNDOS
        )
    except requests.RequestException as e:
        logger.error("Falha de rede em GET %s: %s", caminho, e)
        raise ContaAzulAPIError(None, f"Falha de rede ao chamar a Conta Azul ({caminho}).") from e

    return _tratar_resposta(resp, caminho)


def get_todas_paginas(
    access_token: str,
    caminho: str,
    chave_itens: str,
    tamanho_pagina: int = 200,
    max_paginas: int = 50,
) -> list[dict]:
    """Pagina um endpoint de listagem da Conta Azul até a API devolver uma
    página vazia ou incompleta (menos itens que `tamanho_pagina`), sinal de
    que chegou ao fim.

    `chave_itens` varia por endpoint - a API da Conta Azul não é consistente
    nisso: /v1/categorias, /v1/conta-financeira e /v1/centro-de-custo usam
    "itens", mas /v1/pessoas usa "items". Confirme o nome correto por
    endpoint antes de usar (ver chamadas em agente_integrado.py e
    painel_revisao.py).

    `max_paginas` é um limite de segurança (50 páginas = até 10 mil itens
    com o tamanho padrão) para nunca entrar em loop infinito caso a API
    devolva algo inesperado (ex: sempre a mesma página cheia por engano)."""
    todos_itens: list[dict] = []
    pagina = 1

    while pagina <= max_paginas:
        resposta = get(access_token, caminho, params={"pagina": pagina, "tamanho_pagina": tamanho_pagina})
        itens_pagina = resposta.get(chave_itens, [])
        todos_itens.extend(itens_pagina)

        if len(itens_pagina) < tamanho_pagina:
            break
        pagina += 1
    else:
        logger.warning(
            "get_todas_paginas atingiu o limite de segurança de %s páginas em %s - "
            "pode haver mais itens não lidos.", max_paginas, caminho,
        )

    return todos_itens


def post(access_token: str, caminho: str, payload: dict) -> dict[str, Any]:
    return _escrever("POST", access_token, caminho, payload)


def put(access_token: str, caminho: str, payload: dict) -> dict[str, Any]:
    """AVISO: método PUT ainda não confirmado contra a API real da Conta
    Azul (não há documentação pública acessível para "atualizar contas a
    pagar" - buscas e um catálogo de ferramentas de terceiros só mostram
    criar/listar/buscar por id/quitar, nunca atualizar). Implementado por
    convenção REST (mesmo padrão de `post`, mas com verbo PUT e o id do
    recurso no path) - precisa ser validado contra uma chamada real antes
    de confiar neste caminho em produção."""
    return _escrever("PUT", access_token, caminho, payload)


def patch(access_token: str, caminho: str, payload: dict) -> dict[str, Any]:
    """CONFIRMADO ao vivo (2026-09-08, criando e atualizando um lançamento
    de teste real) contra `/v1/financeiro/eventos-financeiros/parcelas/{id}`
    - atualiza banco/forma de pagamento/vencimento de uma parcela já
    existente. Exige `versao` (concorrência otimista - a Conta Azul
    rejeita se vier desatualizada) e `composicao_valor.valor_bruto`
    (obrigatório mesmo sem mudar o valor) - leia a parcela primeiro com
    `get()` no mesmo path para pegar os dois."""
    return _escrever("PATCH", access_token, caminho, payload)


def _escrever(metodo: str, access_token: str, caminho: str, payload: dict) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = _sessao_escrita.request(
            metodo, _url(caminho), headers=headers, json=payload, timeout=config.HTTP_TIMEOUT_SEGUNDOS
        )
    except requests.ConnectionError as e:
        # Inclui ConnectTimeout: a requisição não chegou a sair - nada foi gravado.
        logger.error("Falha de conexão em %s %s: %s", metodo, caminho, e)
        raise ContaAzulAPIError(None, f"Falha de rede ao chamar a Conta Azul ({caminho}).") from e
    except requests.RequestException as e:
        # ReadTimeout e afins: a Conta Azul pode ter recebido e gravado.
        logger.error("Sem resposta em %s %s (pode ter sido gravado): %s", metodo, caminho, e)
        raise ContaAzulAPIError(
            None,
            f"A Conta Azul não respondeu a tempo ao chamar '{caminho}'. O lançamento PODE ter sido "
            f"gravado - confira na Conta Azul antes de enviar de novo.",
            resultado_incerto=True,
        ) from e

    return _tratar_resposta(resp, caminho, escrita=True)


def _tratar_resposta(resp: requests.Response, caminho: str, escrita: bool = False) -> dict[str, Any]:
    # Log completo do corpo só em DEBUG (nunca em INFO/produção) - pode
    # conter dados financeiros/pessoais.
    logger.debug("Resposta %s %s: status=%s", caminho, resp.request.method, resp.status_code)

    if resp.status_code >= 400:
        # Mensagem segura: não repassamos o JSON bruto de erro para quem
        # não deveria ver (ex: painel de revisão exibido a usuários não-admin).
        logger.error(
            "Erro %s da Conta Azul em %s. Corpo (DEBUG apenas): %s",
            resp.status_code, caminho, resp.text[:2000],
        )
        if escrita and resp.status_code >= 500:
            # 5xx em escrita: o erro pode ter acontecido depois de gravar.
            raise ContaAzulAPIError(
                resp.status_code,
                f"A Conta Azul retornou erro {resp.status_code} ao chamar '{caminho}'. O lançamento "
                f"PODE ter sido gravado - confira na Conta Azul antes de enviar de novo.",
                resultado_incerto=True,
            )
        raise ContaAzulAPIError(
            resp.status_code,
            f"A Conta Azul retornou erro {resp.status_code} ao chamar '{caminho}'. "
            f"Verifique os logs (nível DEBUG) para detalhes.",
        )

    try:
        return resp.json()
    except ValueError as e:
        logger.error("Resposta de %s não é um JSON válido.", caminho)
        raise ContaAzulAPIError(resp.status_code, f"Resposta inválida (não-JSON) de '{caminho}'.") from e