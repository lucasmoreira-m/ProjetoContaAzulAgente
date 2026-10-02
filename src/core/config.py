"""
Configuração centralizada do projeto.

Correções aplicadas aqui (ver relatório de auditoria):
- Todas as variáveis de ambiente obrigatórias são validadas UMA VEZ, na
  importação deste módulo, com mensagem de erro clara se faltar alguma.
  Antes, cada módulo fazia `os.getenv("X").strip()` direto: se a variável
  não existisse, isso quebrava com `AttributeError: 'NoneType' object has
  no attribute 'strip'`, uma mensagem que não diz ao usuário QUAL variável
  está faltando.
- Nenhum segredo é logado, nem mesmo em modo debug.
- Constantes de negócio (ex: CONTA_FINANCEIRA_ID) que antes estavam
  hardcoded no meio de agente_integrado.py agora vivem aqui, num único
  lugar (ver escopo 5 e 7 da auditoria: config centralizada).

ATENÇÃO - premissa assumida (não havia contexto sobre isso no projeto):
Como não foi indicado o uso de um gerenciador de segredos corporativo
(AWS Secrets Manager / Azure Key Vault / HashiCorp Vault), mantive o padrão
de mercado mais simples e já usado no projeto (.env + python-dotenv),
apenas endurecendo a validação e a forma como o token é persistido.
Se a empresa já usa (ou pretende usar) um gerenciador de segredos, isso
deve ser adaptado aqui antes de ir para produção - é só um ponto de
entrada, então a migração é local a este arquivo.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# CORREÇÃO (2026-08-28): resolve um problema real observado em produção -
# quando o projeto inteiro (incluindo o .env) fica dentro de uma pasta
# sincronizada pelo OneDrive compartilhada entre duas máquinas/pessoas, o
# OneDrive sincroniza o .env também - e cada máquina precisa de um
# .env DIFERENTE (credenciais próprias) quando cada uma tem seu próprio
# app/token cadastrado na Conta Azul, para nunca disputar o mesmo
# refresh_token entre si. Sincronizar o .env nesse cenário faz uma máquina
# sobrescrever o token da outra sem ninguém perceber - foi exatamente
# isso que causou uma corrida real (erro 400 invalid_grant) nesta sessão.
#
# Solução: por padrão, procura o .env numa pasta LOCAL fixa, fora de
# qualquer pasta do OneDrive - "~/ContaAzulConfig/.env" (o "~" expande
# para a pasta de usuário de cada máquina, ex: C:\Users\Fulano, que fica
# um nível ACIMA de "OneDrive - Empresa", logo nunca é sincronizada). Cada
# máquina/pessoa mantém seu próprio arquivo ali, com suas próprias
# credenciais - o código (compartilhado/sincronizado normalmente) é o
# mesmo para todos, só o .env de cada um é local e independente.
#
# Se esse arquivo não existir (ex: instalação de máquina única, sem esse
# cenário de duas pessoas), cai de volta no comportamento padrão anterior
# (.env na pasta do projeto) - não quebra setups existentes.
_CAMINHO_ENV_LOCAL = Path(os.path.expanduser("~")) / "ContaAzulConfig" / ".env"

# secrets.toml (2026-10-01, pedido do usuário - mesmo formato do outro
# sistema da empresa): alternativa ao .env, lida ANTES dele e com prioridade
# sobre ele. As seções ([conta_azul], [microsoft]...) são só organização:
# cada chave vira a variável de ambiente de mesmo nome. Valor vazio é
# ignorado (deixa o .env completar). Variável já definida no ambiente
# (Cloud Run, Docker -e) nunca é sobrescrita.
CAMINHO_SECRETS_TOML = Path(os.getenv("SECRETS_TOML_PATH", "secrets.toml"))
CHAVES_DO_SECRETS_TOML: set[str] = set()


def _carregar_secrets_toml() -> None:
    import tomllib

    # utf-8-sig: aceita o arquivo com BOM (o Bloco de Notas e o PowerShell
    # do Windows costumam gravar assim), que o tomllib recusaria.
    conteudo = tomllib.loads(CAMINHO_SECRETS_TOML.read_text(encoding="utf-8-sig"))

    def _achatar(tabela):
        for chave, valor in tabela.items():
            if isinstance(valor, dict):
                yield from _achatar(valor)
            else:
                yield chave, valor

    for chave, valor in _achatar(conteudo):
        nome = str(chave).strip().upper()
        if isinstance(valor, list):
            valor = ",".join(str(v) for v in valor)
        valor = str(valor).strip()
        if valor and nome not in os.environ:
            os.environ[nome] = valor
            CHAVES_DO_SECRETS_TOML.add(nome)


if CAMINHO_SECRETS_TOML.exists():
    _carregar_secrets_toml()

if _CAMINHO_ENV_LOCAL.exists():
    load_dotenv(dotenv_path=_CAMINHO_ENV_LOCAL)
else:
    # Comportamento original: procura .env na pasta do projeto (ou pais).
    # Os outros módulos NÃO devem chamar load_dotenv() de novo (evita
    # comportamento inconsistente entre módulos).
    load_dotenv()

# Azure (2026-09-30): no container do Azure não existe .env - as
# credenciais vêm do Key Vault (seu-keyvault), lidas com a identidade gerenciada
# do container (sem senha nenhuma na imagem nem na configuração). Só entra
# em ação se AZURE_KEY_VAULT_URL estiver definida; o .exe local não define
# e continua exatamente como antes. Os segredos são copiados para
# os.environ uma vez, na importação, para o resto do código (que já lê
# tudo via os.getenv) não precisar saber de onde vieram. Nomes com hífen
# porque o Key Vault não aceita "_" em nome de segredo.
SEGREDOS_KEY_VAULT = {
    "conta-azul-client-id": "CONTA_AZUL_CLIENT_ID",
    "conta-azul-client-secret": "CONTA_AZUL_CLIENT_SECRET",
    "conta-azul-refresh-token": "CONTA_AZUL_REFRESH_TOKEN",
    "gemini-api-key": "GEMINI_API_KEY",
}
SEGREDO_REFRESH_TOKEN_KEY_VAULT = "conta-azul-refresh-token"
AZURE_KEY_VAULT_URL = os.getenv("AZURE_KEY_VAULT_URL", "").strip()


def usando_key_vault() -> bool:
    return bool(AZURE_KEY_VAULT_URL)


def cliente_key_vault():
    """SecretClient do Key Vault. Import tardio: o .exe local não precisa
    (nem empacota) as bibliotecas do Azure."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=AZURE_KEY_VAULT_URL, credential=DefaultAzureCredential())


def _carregar_segredos_do_key_vault() -> None:
    cliente = cliente_key_vault()
    for nome_segredo, nome_env in SEGREDOS_KEY_VAULT.items():
        os.environ[nome_env] = cliente.get_secret(nome_segredo).value


if usando_key_vault():
    _carregar_segredos_do_key_vault()


# Google Cloud (2026-10-01): os MESMOS segredos do Key Vault, com os mesmos
# nomes, no Secret Manager do projeto em GOOGLE_SECRET_MANAGER_PROJETO. Lidos
# com a conta de serviço do Cloud Run (ou o login do gcloud no PC). O
# refresh_token renovado é gravado de volta como versão nova do segredo -
# o disco do Cloud Run se perde a cada reinício. Usa a API REST com o
# google-auth que já vem com o google-genai (sem biblioteca nova).
GOOGLE_SECRET_MANAGER_PROJETO = os.getenv("GOOGLE_SECRET_MANAGER_PROJETO", "").strip()
# Opcionais: carregados só se existirem no Secret Manager.
SEGREDOS_OPCIONAIS_GOOGLE = {"ms-client-secret": "MS_CLIENT_SECRET", "sessao-chave": "SESSAO_CHAVE"}
# O Secret Manager cobra por versão ativa: depois de gravar um token novo,
# as versões mais antigas que estas são destruídas.
VERSOES_MANTIDAS_SECRET_MANAGER = 2


def usando_secret_manager() -> bool:
    return bool(GOOGLE_SECRET_MANAGER_PROJETO)


def _sessao_google():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credenciais, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credenciais)


def _url_segredo_google(nome: str) -> str:
    return f"https://secretmanager.googleapis.com/v1/projects/{GOOGLE_SECRET_MANAGER_PROJETO}/secrets/{nome}"


def ler_segredo_google(nome: str, sessao=None) -> str | None:
    """Valor da versão mais recente; None se o segredo não existir."""
    resposta = (sessao or _sessao_google()).get(f"{_url_segredo_google(nome)}/versions/latest:access", timeout=20)
    if resposta.status_code == 404:
        return None
    resposta.raise_for_status()
    return base64.b64decode(resposta.json()["payload"]["data"]).decode("utf-8")


def gravar_segredo_google(nome: str, valor: str) -> None:
    """Adiciona a versão nova e destrói as antigas além das mantidas."""
    sessao = _sessao_google()
    url = _url_segredo_google(nome)
    dados = base64.b64encode(valor.encode("utf-8")).decode("ascii")
    sessao.post(f"{url}:addVersion", json={"payload": {"data": dados}}, timeout=20).raise_for_status()
    try:
        resposta = sessao.get(f"{url}/versions", params={"filter": "state:ENABLED"}, timeout=20)
        resposta.raise_for_status()
        versoes = sorted(resposta.json().get("versions", []), key=lambda v: v.get("createTime", ""), reverse=True)
        for versao in versoes[VERSOES_MANTIDAS_SECRET_MANAGER:]:
            sessao.post(f"https://secretmanager.googleapis.com/v1/{versao['name']}:destroy", json={}, timeout=20)
    except Exception:
        # Limpeza é economia, não correção: a versão nova já foi gravada.
        logging.getLogger(__name__).warning("Não foi possível apagar versões antigas do segredo %s.", nome)


def _carregar_segredos_do_secret_manager() -> None:
    """Cada segredo pode vir do Secret Manager OU já ter vindo do
    secrets.toml (montado como arquivo no Cloud Run). O refresh_token é a
    exceção: sempre do Secret Manager, porque o arquivo montado é só leitura
    e o token renovado precisa ser gravado em algum lugar."""
    sessao = _sessao_google()
    faltando = []
    for nome_segredo, nome_env in SEGREDOS_KEY_VAULT.items():
        valor = ler_segredo_google(nome_segredo, sessao)
        if valor is not None:
            os.environ[nome_env] = valor.strip()
        elif nome_segredo == SEGREDO_REFRESH_TOKEN_KEY_VAULT or not os.environ.get(nome_env):
            faltando.append(nome_segredo)
    if faltando:
        raise RuntimeError(
            f"Segredo(s) não encontrado(s) no Secret Manager do projeto {GOOGLE_SECRET_MANAGER_PROJETO}: "
            f"{', '.join(faltando)}. Crie com exatamente esses nomes (os outros também podem vir do "
            "secrets.toml; o conta-azul-refresh-token precisa estar no Secret Manager)."
        )
    for nome_segredo, nome_env in SEGREDOS_OPCIONAIS_GOOGLE.items():
        valor = ler_segredo_google(nome_segredo, sessao)
        if valor is not None:
            os.environ[nome_env] = valor.strip()


if usando_secret_manager():
    _carregar_segredos_do_secret_manager()


# Travas de arquivo (2026-10-01): no Google o volume é um bucket do Cloud
# Storage, que não tem trava de arquivo. Com PASTA_TRAVAS (ligada no
# Dockerfile: /tmp/travas) as travas ficam no disco local do container, fora
# do volume - basta, porque o painel roda numa instância só. Vazia (PC), a
# trava fica ao lado do arquivo, como sempre foi.
PASTA_TRAVAS = os.getenv("PASTA_TRAVAS", "").strip()


def caminho_trava(caminho) -> str:
    if not PASTA_TRAVAS:
        return f"{caminho}.lock"
    os.makedirs(PASTA_TRAVAS, exist_ok=True)
    absoluto = os.path.abspath(str(caminho))
    resumo = hashlib.sha1(absoluto.encode("utf-8")).hexdigest()[:12]
    return os.path.join(PASTA_TRAVAS, f"{resumo}_{os.path.basename(absoluto)}.lock")

# REMOVIDO (2026-09-24): o fallback "env_embutido.py" (credenciais geradas
# por config_build.py e embutidas no .exe). O PyInstaller copiava o arquivo
# como código-fonte legível para dist/app/_internal/src/core/ - qualquer
# pessoa com a pasta do app (ou um zip de atualização) lia client_secret,
# refresh_token e a chave do Gemini no Bloco de Notas. Credenciais agora
# vêm SÓ de um .env em disco, por máquina (~/ContaAzulConfig/.env, ou .env
# ao lado do app.exe), nunca de dentro do pacote distribuído.

# ---------------------------------------------------------------------------
# Logging estruturado (substitui os prints espalhados pelo projeto)
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# CORREÇÃO (2026-09-03, encontrada testando "app.exe monitor" no .exe
# compilado): várias mensagens de log usam emoji (ex: "🔴 AMBIENTE:
# PRODUÇÃO REAL..."). No Windows, quando a saída não é um console UTF-8 de
# verdade (ex: redirecionada para arquivo/serviço em segundo plano, como o
# monitoramento roda), sys.stdout usa o codepage padrão do sistema
# (cp1252), que não consegue codificar emoji - e cada uma dessas linhas
# quebrava com UnicodeEncodeError (silenciosamente engolido pelo próprio
# módulo logging, mas nunca mostrando a mensagem de fato). Força UTF-8 com
# fallback tolerante (nunca lança, na pior hipótese substitui o caractere)
# antes de configurar o logging - reconfigure() só existe em streams reais
# (Python 3.7+), daí o hasattr de defesa.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)

# Bibliotecas de terceiro usam o módulo `logging` padrão, então herdam a
# configuração do logger raiz acima (mesmo nível/formato dos nossos módulos).
# São verbosas em INFO (uma linha por requisição HTTP) sem agregar nada útil
# no dia a dia - por isso ficam travadas em WARNING aqui, independente do
# LOG_LEVEL do projeto. Se precisar depurar uma chamada específica ao Gemini
# (ex: erro de rede difícil de reproduzir), suba isso para INFO/DEBUG
# temporariamente só nessas linhas, sem precisar mudar o LOG_LEVEL geral.
for _nome_lib in ("httpx", "httpcore", "google_genai", "urllib3"):
    logging.getLogger(_nome_lib).setLevel(logging.WARNING)


def get_logger(nome: str) -> logging.Logger:
    return logging.getLogger(nome)


# ---------------------------------------------------------------------------
# Variáveis de ambiente obrigatórias
# ---------------------------------------------------------------------------
_OBRIGATORIAS = [
    "CONTA_AZUL_CLIENT_ID",
    "CONTA_AZUL_CLIENT_SECRET",
    "CONTA_AZUL_REFRESH_TOKEN",
    "GEMINI_API_KEY",
    "CONTA_AZUL_AMBIENTE",
]

_VALORES_VALIDOS_AMBIENTE = {"teste", "producao"}


def _obter_env_obrigatoria(nome: str) -> str:
    valor = os.getenv(nome)
    if valor is None or not valor.strip():
        raise RuntimeError(
            f"Variável de ambiente obrigatória '{nome}' não foi definida (ou está vazia). "
            f"Verifique o arquivo .env na raiz do projeto. "
            f"Use .env.example como referência."
        )
    return valor.strip()


def validar_configuracao() -> None:
    """Falha rápido e com mensagem clara se alguma variável obrigatória faltar.
    Chame isso no início de qualquer ponto de entrada (main, painel, monitor)."""
    faltando = []
    for nome in _OBRIGATORIAS:
        try:
            _obter_env_obrigatoria(nome)
        except RuntimeError:
            faltando.append(nome)
    if faltando:
        raise RuntimeError(
            "Configuração incompleta. As seguintes variáveis de ambiente estão "
            f"faltando ou vazias: {', '.join(faltando)}. As credenciais NÃO vêm "
            f"mais dentro do app - crie o arquivo {_CAMINHO_ENV_LOCAL} (recomendado, "
            "fora do OneDrive) ou um .env na raiz do projeto, no formato "
            "do .env.example. No container do Azure, defina AZURE_KEY_VAULT_URL "
            "(os segredos vêm do Key Vault); para só testar a imagem localmente, "
            "passe valores falsos com -e e CONTA_AZUL_AMBIENTE=teste (ver README, "
            "\"Hospedar no Azure\")."
        )

    ambiente = os.getenv("CONTA_AZUL_AMBIENTE", "").strip().lower()
    if ambiente not in _VALORES_VALIDOS_AMBIENTE:
        raise RuntimeError(
            f"CONTA_AZUL_AMBIENTE deve ser exatamente 'teste' ou 'producao' (veio '{ambiente}'). "
            f"Isso evita confusão sobre qual conta da Conta Azul está sendo usada - defina "
            f"explicitamente no .env, sem valor padrão silencioso."
        )


# Carregados sob demanda (não no import, para permitir que quem só quer
# `regras_rateio.py` por exemplo não precise ter .env configurado).
def gemini_api_key() -> str:
    return _obter_env_obrigatoria("GEMINI_API_KEY")


def conta_azul_client_id() -> str:
    return _obter_env_obrigatoria("CONTA_AZUL_CLIENT_ID")


def conta_azul_client_secret() -> str:
    return _obter_env_obrigatoria("CONTA_AZUL_CLIENT_SECRET")


def conta_azul_refresh_token() -> str:
    return _obter_env_obrigatoria("CONTA_AZUL_REFRESH_TOKEN")


def conta_azul_ambiente() -> str:
    """'teste' ou 'producao' - declarado explicitamente no .env, nunca
    adivinhado. Usado para exibir um aviso bem visível (log e painel) sobre
    qual conta está sendo usada, depois de uma sessão real onde essa
    confusão quase causou um lançamento duplicado na conta errada."""
    valor = _obter_env_obrigatoria("CONTA_AZUL_AMBIENTE").lower()
    if valor not in _VALORES_VALIDOS_AMBIENTE:
        raise RuntimeError(f"CONTA_AZUL_AMBIENTE deve ser 'teste' ou 'producao' (veio '{valor}').")
    return valor


# ---------------------------------------------------------------------------
# Constantes de negócio / configuração da aplicação
# (antes hardcoded dentro de agente_integrado.py)
# ---------------------------------------------------------------------------
CONTA_AZUL_BASE_URL = "https://api-v2.contaazul.com"
CONTA_AZUL_OAUTH_URL = f"{CONTA_AZUL_BASE_URL}/oauth/token"

# ID da conta financeira padrão usada quando a regra de negócio não define
# uma conta financeira específica (ex: baseada no banco do arquivo de folha).
CONTA_FINANCEIRA_ID_PADRAO = os.getenv(
    "CONTA_FINANCEIRA_ID_PADRAO", "00000000-0000-0000-0000-00000000000a"  # substitua pelo id da sua conta principal
)

# Conta financeira Beta Matriz - usada pelo agente de NFS-e (src/utils/
# regras_nfse.py) quando a árvore de decisão de banco resolve "Beta" (nota
# sem boleto, fornecedor sem conta Banco Alfa aprendida). IDs confirmados ao vivo
# via GET /v1/conta-financeira em 2026-09-08.
CONTA_FINANCEIRA_ID_BETA = os.getenv(
    "CONTA_FINANCEIRA_ID_BETA", "00000000-0000-0000-0000-00000000000b"  # substitua pelo id da sua conta secundária
)

# Trocado de "gemini-3.6-flash" para "gemini-3.5-flash-lite" (2026-09-21,
# pedido explícito do usuário) por dois motivos, os dois confirmados num
# teste real com documentos de produção (não só achismo):
# 1. O preço promocional do 3.6 Flash vale só até dezembro/2026 - o 3.5
#    Flash-Lite já custa perto disso (US$0,30/US$2,50 por 1M tokens contra
#    US$1,50/US$7,50 do 3.6 Flash) e continua barato depois da promoção
#    acabar, sem precisar trocar de novo às pressas.
# 2. Teste comparativo (4 documentos reais de folha de pagamento) mostrou o
#    3.6 Flash falhando nas 4 tentativas (503 "high demand" + 429 de cota
#    diária excedida), enquanto o 3.5 Flash-Lite respondeu com sucesso nas
#    4, rápido, com dados batendo com os valores esperados. Ou seja, além
#    de mais barato, hoje é o mais confiável dos dois pra essa tarefa de
#    extração estruturada.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Teto de valor para lançamento AUTOMÁTICO de documento de fornecedor.
# Acima do teto, o documento vai sempre para revisão humana, mesmo com tudo
# resolvendo - medida de segurança contra documento adulterado (ver
# src/utils/deteccao_injecao.py). Calibre logo acima dos maiores valores
# RECORRENTES do seu histórico de contas a pagar, para não travar o dia a
# dia (os padrões abaixo são só exemplo). Folha, encargos e rescisão NÃO têm
# teto: vêm do Departamento Pessoal, não de fornecedor externo.
TETO_AUTONOMIA_NFSE = float(os.getenv("TETO_AUTONOMIA_NFSE", "45000"))
TETO_AUTONOMIA_DOCUMENTO_COMUM = float(os.getenv("TETO_AUTONOMIA_DOCUMENTO_COMUM", "16000"))
# Fornecedores isentos do teto, por CNPJ (só dígitos): fornecedores de valor
# alto e recorrente (ex.: plano de saúde da empresa) que sempre passariam do
# teto. As demais proteções continuam valendo para eles (injeção, duplicata,
# maturidade do fornecedor). CNPJ e não nome: nome é fácil de imitar.
FORNECEDORES_ISENTOS_TETO_AUTONOMIA = {
    "44555666000177": "Saúde Exemplo S.A.",
}

# HTTP: timeouts e retry (Escopo 2 da auditoria)
HTTP_TIMEOUT_SEGUNDOS = float(os.getenv("HTTP_TIMEOUT_SEGUNDOS", "20"))
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
HTTP_BACKOFF_FACTOR = float(os.getenv("HTTP_BACKOFF_FACTOR", "1.5"))

# Pastas de trabalho do monitor
PASTA_ENTRADA = os.getenv("PASTA_ENTRADA", "entrada")
PASTA_PROCESSADOS = os.getenv("PASTA_PROCESSADOS", "processados")
PASTA_REVISAO = os.getenv("PASTA_REVISAO", "revisao")
PASTA_DESCARTADOS = os.getenv("PASTA_DESCARTADOS", "descartados")

# Onde o botão "Processar entrada" do painel roda o lote (ver
# src/agentes/processador.py): "local" (este PC) ou "azure" (futuro).
PROCESSADOR = os.getenv("PROCESSADOR", "local").strip().lower()

# Painel no Azure (2026-10-01): "1" exige o login Microsoft 365 (Easy Auth)
# em toda página - ligado no Dockerfile. Local fica desligado.
EXIGIR_LOGIN_M365 = os.getenv("EXIGIR_LOGIN_M365", "").strip() == "1"

# Login Microsoft 365 dentro do painel (2026-10-01, src/web/login_microsoft.py)
# - para rodar fora do Azure, onde não há Easy Auth. Liga sozinho com as três
# primeiras preenchidas (registro do app no Entra ID). MS_REDIRECT_URI só é
# necessária se o endereço público for diferente do que o painel enxerga.
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "").strip()
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "").strip()
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "").strip()
MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "").strip()
LOGIN_MICROSOFT = bool(MS_CLIENT_ID and MS_TENANT_ID and MS_CLIENT_SECRET)
# E-mails liberados, separados por vírgula. Vazia = qualquer conta da empresa.
USUARIOS_PERMITIDOS = {e.strip().lower() for e in os.getenv("USUARIOS_PERMITIDOS", "").split(",") if e.strip()}
# Chave que assina o cookie de sessão. Sem ela, gera uma aleatória ao subir
# (todo mundo precisa entrar de novo a cada reinício do painel).
SESSAO_CHAVE = os.getenv("SESSAO_CHAVE", "").strip()

# Caminho do arquivo .env, usado apenas pelo módulo de auth para persistir
# o refresh_token renovado.
# Caminho do arquivo .env, usado apenas pelo módulo de auth para persistir
# o refresh_token renovado. Aponta para o mesmo arquivo que foi
# efetivamente carregado acima (local fora do OneDrive, se existir; senão
# o .env da pasta do projeto) - importante para o token renovado ser
# regravado no MESMO lugar de onde veio, não em outro.
ENV_FILE_PATH = _CAMINHO_ENV_LOCAL if _CAMINHO_ENV_LOCAL.exists() else Path(os.getenv("ENV_FILE_PATH", ".env"))

# Caminho do registro local de fornecedores "aprendidos" (ver fornecedores_registry.py)
REGISTRO_FORNECEDORES_PATH = os.getenv("REGISTRO_FORNECEDORES_PATH", "fornecedores_registry.json")

# Caminho do registro local de centro de custo por funcionário, escolhido
# manualmente no painel (ver historico_centros_funcionarios.py)
HISTORICO_CENTROS_FUNCIONARIOS_PATH = os.getenv(
    "HISTORICO_CENTROS_FUNCIONARIOS_PATH", "historico_centros_funcionarios.json"
)

# Cache compartilhado do access_token (ver conta_azul_auth.py) - resolve a
# corrida entre processos (ex: monitor_pasta.py + painel_revisao.py rodando
# ao mesmo tempo) que antes disputavam o mesmo refresh_token de uso único.
CACHE_ACCESS_TOKEN_PATH = os.getenv("CACHE_ACCESS_TOKEN_PATH", ".access_token_cache.json")
CACHE_ACCESS_TOKEN_LOCK_PATH = os.getenv("CACHE_ACCESS_TOKEN_LOCK_PATH", ".access_token_cache.lock")
# Margem de segurança: renova antes do vencimento real, nunca depois.
MARGEM_SEGUNDOS_RENOVACAO_TOKEN = int(os.getenv("MARGEM_SEGUNDOS_RENOVACAO_TOKEN", "60"))
# Usado só se a resposta da Conta Azul não trouxer "expires_in" (defensivo -
# prefere renovar cedo demais a usar um token vencido por tempo demais).
DURACAO_PADRAO_TOKEN_SEGUNDOS = int(os.getenv("DURACAO_PADRAO_TOKEN_SEGUNDOS", "300"))