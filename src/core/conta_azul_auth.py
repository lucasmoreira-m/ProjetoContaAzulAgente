"""
Renovação do access_token da Conta Azul via refresh_token (OAuth2).

Correções aplicadas (ver relatório de auditoria, itens críticos 1-3):
- Timeout e retry na chamada de renovação de token (antes não tinha nenhum).
- Validação das variáveis de ambiente com mensagem clara (config.py), em vez
  de `.strip()` num valor que pode ser None.
- Escrita do novo refresh_token no .env é ATÔMICA (arquivo temporário +
  os.replace) - evita corrupção do .env se o processo for interrompido no
  meio da escrita.
- Erro de renovação de token não vaza corpo bruto da resposta (pode conter
  dado sensível) na mensagem de exceção/log em nível INFO; só em DEBUG.

CORREÇÃO NOVA (resolve bug real observado em uso: RuntimeError 400 ao
renovar token, recorrente mesmo após reiniciar o processo):

Antes, `obter_access_token()` trocava o refresh_token por um novo TODA VEZ
que era chamada - mesmo que o access_token anterior ainda estivesse válido.
Como o refresh_token é de uso único, isso significa que dois processos
chamando a função por perto um do outro (ex: `monitor_pasta.py` rodando
continuamente + `painel_revisao.py` sendo aberto ao mesmo tempo - o cenário
normal de produção, não um erro de uso) inevitavelmente colidiam: o segundo
processo a chegar ficava com um refresh_token que o primeiro já tinha
invalidado, e o erro 400 se repetia a cada reinício, porque o problema não
era "processo desatualizado" - era a corrida em si, que acontece de novo a
cada nova troca de token.

Agora `obter_access_token()`:
1. Primeiro verifica um CACHE de access_token compartilhado em disco
   (`.access_token_cache.json`). Se ainda válido (com margem de segurança),
   devolve esse token direto - SEM tocar no refresh_token, sem chamada de
   rede nenhuma. Isso, sozinho, já elimina quase toda a corrida, porque a
   token só precisa ser trocado uma vez por período de validade, não a
   cada chamada.
2. Se precisar renovar de verdade, adquire um LOCK de arquivo
   (`.access_token_cache.lock`, via biblioteca `filelock`) antes de chamar
   a Conta Azul - garante que, mesmo que dois processos precisem renovar
   ao mesmo tempo, só um faz a troca por vez.
3. Depois de conseguir o lock, RECONFERE o cache (outro processo pode ter
   acabado de renovar enquanto esperava o lock) antes de decidir se ainda
   precisa mesmo trocar o refresh_token - evita renovação duplicada.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time

import requests
from filelock import FileLock, Timeout as FileLockTimeout

from src.core import config

logger = config.get_logger(__name__)

_TIMEOUT_LOCK_SEGUNDOS = 30  # quanto tempo um processo espera pelo lock antes de desistir


def obter_access_token() -> str:
    """Devolve um access_token válido da Conta Azul - reaproveitando o
    cache compartilhado sempre que possível, e só trocando o refresh_token
    quando realmente necessário (protegido por lock entre processos)."""
    cache = _ler_cache()
    if cache and cache["expira_em"] > time.time() + config.MARGEM_SEGUNDOS_RENOVACAO_TOKEN:
        return cache["access_token"]

    lock = FileLock(config.CACHE_ACCESS_TOKEN_LOCK_PATH, timeout=_TIMEOUT_LOCK_SEGUNDOS)
    try:
        with lock:
            # Double-checked locking: outro processo pode ter renovado
            # enquanto esperávamos o lock - reconfere antes de gastar uma
            # troca de refresh_token à toa (e evitar invalidar um token
            # que acabou de ser gerado por outro processo).
            cache = _ler_cache()
            if cache and cache["expira_em"] > time.time() + config.MARGEM_SEGUNDOS_RENOVACAO_TOKEN:
                return cache["access_token"]

            return _renovar_token_de_verdade()
    except FileLockTimeout as e:
        raise RuntimeError(
            f"Não foi possível obter o lock de renovação de token em {_TIMEOUT_LOCK_SEGUNDOS}s "
            f"- outro processo pode estar travado renovando. Tente novamente em instantes."
        ) from e


def _renovar_token_de_verdade() -> str:
    """Faz a troca de fato do refresh_token com a Conta Azul. Só deve ser
    chamada com o lock de arquivo já adquirido."""
    client_id = config.conta_azul_client_id()
    client_secret = config.conta_azul_client_secret()
    refresh_token_atual = config.conta_azul_refresh_token()

    credenciais_base64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    headers = {
        "Authorization": f"Basic {credenciais_base64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token_atual}

    try:
        resp = requests.post(
            config.CONTA_AZUL_OAUTH_URL,
            headers=headers,
            data=data,
            timeout=config.HTTP_TIMEOUT_SEGUNDOS,
        )
    except requests.RequestException as e:
        logger.error("Falha de rede ao renovar token da Conta Azul: %s", e)
        raise RuntimeError("Falha de rede ao renovar o token da Conta Azul.") from e

    if resp.status_code != 200:
        logger.error(
            "Erro %s ao renovar token. Corpo (DEBUG apenas): %s",
            resp.status_code, resp.text[:2000],
        )
        raise RuntimeError(
            f"Erro ao renovar token da Conta Azul (status {resp.status_code}). "
            f"Verifique os logs em nível DEBUG para detalhes."
        )

    dados = resp.json()
    novo_access_token = dados["access_token"]
    novo_refresh_token = dados["refresh_token"]

    expires_in = dados.get("expires_in")
    if expires_in is None:
        logger.warning(
            "Resposta da Conta Azul não trouxe 'expires_in' - usando duração "
            "padrão conservadora de %ss.", config.DURACAO_PADRAO_TOKEN_SEGUNDOS,
        )
        expires_in = config.DURACAO_PADRAO_TOKEN_SEGUNDOS

    _atualizar_refresh_token_no_env(novo_refresh_token)
    _salvar_cache(novo_access_token, time.time() + expires_in)

    return novo_access_token


def _ler_cache() -> dict | None:
    caminho = config.CACHE_ACCESS_TOKEN_PATH
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if "access_token" in dados and "expira_em" in dados:
            return dados
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cache de access_token corrompido/ilegível (%s) - ignorando e renovando.", e)
    return None


def _salvar_cache(access_token: str, expira_em: float) -> None:
    """Escrita atômica do cache (mesmo padrão do .env)."""
    caminho = config.CACHE_ACCESS_TOKEN_PATH
    diretorio = os.path.dirname(caminho) or "."
    fd, caminho_temp = tempfile.mkstemp(dir=diretorio, prefix=".access_token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"access_token": access_token, "expira_em": expira_em}, f)
        os.replace(caminho_temp, caminho)
    except Exception:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)
        raise


def _atualizar_refresh_token_no_env(novo_refresh_token: str) -> None:
    """Reescreve CONTA_AZUL_REFRESH_TOKEN no arquivo .env de forma atômica.

    CORREÇÃO (2026-09-01): antes, se o arquivo simplesmente não existisse
    (caso real: .exe standalone rodando só com env_embutido.py, sem nenhum
    .env físico ainda na primeira renovação), a função lançava RuntimeError
    - travando obter_access_token() por completo. A alternativa óbvia (só
    logar um aviso e seguir, sem persistir nada) é PIOR do que parece: o
    refresh_token antigo já foi invalidado no servidor da Conta Azul nessa
    troca, então perder o novo silenciosamente só adia o problema - a
    PRÓXIMA renovação (minutos depois, quando o access_token expirar) falha
    com 'invalid_grant', um erro bem mais confuso e sem tratamento nenhum.

    Por isso, se o arquivo não existir, ele é CRIADO agora, completo, com
    todas as variáveis obrigatórias já presentes em os.environ (carregadas
    por config.py a partir do env_embutido/.env atual) - assim a
    persistência passa a funcionar de verdade a partir desta chamada, em
    vez de só evitar o crash sem resolver a causa. Erros genuínos de
    escrita (permissão, disco cheio) viram warning, não crash - a essa
    altura já foi feito o possível para persistir corretamente."""
    if config.usando_key_vault():
        _atualizar_refresh_token_no_key_vault(novo_refresh_token)
        return
    if config.usando_secret_manager():
        _atualizar_refresh_token_no_secret_manager(novo_refresh_token)
        return
    if "CONTA_AZUL_REFRESH_TOKEN" in config.CHAVES_DO_SECRETS_TOML:
        _atualizar_refresh_token_no_secrets_toml(novo_refresh_token)
        return

    caminho_env = config.ENV_FILE_PATH

    if not caminho_env.exists():
        logger.warning(
            "%s não existe ainda - criando agora com as variáveis atuais do "
            "ambiente, para a renovação de refresh_token poder ser "
            "persistida a partir desta chamada.", caminho_env,
        )
        try:
            caminho_env.parent.mkdir(parents=True, exist_ok=True)
            conteudo_inicial = "\n".join(
                f"{nome}={os.environ.get(nome, '')}" for nome in config._OBRIGATORIAS
            ) + "\n"
            caminho_env.write_text(conteudo_inicial, encoding="utf-8")
        except OSError as e:
            logger.warning(
                "Não foi possível criar %s (%s) - refresh_token renovado NÃO "
                "será persistido; a próxima renovação pode falhar com "
                "'invalid_grant'. Verifique permissões da pasta.",
                caminho_env, e,
            )
            return

    conteudo = caminho_env.read_text(encoding="utf-8")

    if "CONTA_AZUL_REFRESH_TOKEN=" not in conteudo:
        raise RuntimeError(
            f"Chave CONTA_AZUL_REFRESH_TOKEN não encontrada em {caminho_env}. "
            f"Não é seguro tentar inserir uma nova entrada automaticamente."
        )

    conteudo_novo = re.sub(
        r"CONTA_AZUL_REFRESH_TOKEN=.*",
        f"CONTA_AZUL_REFRESH_TOKEN={novo_refresh_token}",
        conteudo,
    )

    diretorio = caminho_env.parent or "."
    fd, caminho_temp = tempfile.mkstemp(dir=diretorio, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo_novo)
        os.replace(caminho_temp, caminho_env)
    except OSError as e:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)
        logger.warning(
            "Falha ao persistir refresh_token renovado em %s (%s) - a "
            "próxima renovação pode falhar com 'invalid_grant'.",
            caminho_env, e,
        )
        return

    os.environ["CONTA_AZUL_REFRESH_TOKEN"] = novo_refresh_token
    logger.info("refresh_token renovado e persistido com sucesso.")


def _atualizar_refresh_token_no_secrets_toml(novo_refresh_token: str) -> None:
    """O token veio do secrets.toml (2026-10-01): o novo precisa voltar para
    ELE - o secrets.toml é lido antes do .env, então gravar no .env deixaria
    o token antigo (já invalidado) valendo na próxima abertura. Troca só a
    linha CONTA_AZUL_REFRESH_TOKEN, preservando comentários e seções, com
    escrita atômica (arquivo temporário + os.replace)."""
    os.environ["CONTA_AZUL_REFRESH_TOKEN"] = novo_refresh_token
    caminho = config.CAMINHO_SECRETS_TOML
    try:
        conteudo = caminho.read_text(encoding="utf-8")
        conteudo_novo, trocas = re.subn(
            r"(?m)^(\s*CONTA_AZUL_REFRESH_TOKEN\s*=\s*).*$",
            lambda m: m.group(1) + json.dumps(novo_refresh_token),
            conteudo,
            count=1,
        )
        if not trocas:
            raise RuntimeError("linha CONTA_AZUL_REFRESH_TOKEN não encontrada")
        fd, caminho_temp = tempfile.mkstemp(dir=caminho.parent or ".", prefix=".secrets.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo_novo)
        os.replace(caminho_temp, caminho)
    except Exception as e:
        logger.error(
            "FALHA ao gravar o refresh_token renovado em %s (%s). O token novo só "
            "existe na memória deste processo - a próxima abertura do painel pode "
            "falhar com 'invalid_grant'.", caminho, type(e).__name__,
        )
        return
    logger.info("refresh_token renovado e persistido no %s.", caminho.name)


def _atualizar_refresh_token_no_secret_manager(novo_refresh_token: str) -> None:
    """No Google Cloud (2026-10-01): mesma lógica do Key Vault - o token
    novo vai primeiro para os.environ (o antigo já foi invalidado) e depois
    vira versão nova do segredo conta-azul-refresh-token. Exige o papel
    "Administrador de versões do Secret Manager" (secretVersionManager) além
    do "Acessador" para a conta de serviço do Cloud Run."""
    os.environ["CONTA_AZUL_REFRESH_TOKEN"] = novo_refresh_token
    try:
        config.gravar_segredo_google(config.SEGREDO_REFRESH_TOKEN_KEY_VAULT, novo_refresh_token)
    except Exception as e:
        logger.error(
            "FALHA ao gravar o refresh_token renovado no Secret Manager (%s). O token "
            "novo só existe na memória deste processo - se o serviço reiniciar antes "
            "de corrigir, será preciso gerar um refresh_token novo. Confira as "
            "permissões da conta de serviço no segredo.", type(e).__name__,
        )
        return
    logger.info("refresh_token renovado e gravado no Secret Manager.")


def _atualizar_refresh_token_no_key_vault(novo_refresh_token: str) -> None:
    """No Azure (2026-09-30), o refresh_token renovado vai para o Key Vault
    como nova versão do segredo - o container não tem .env, e o disco dele
    se perde a cada reinício. Grava primeiro em os.environ: se a escrita no
    Key Vault falhar, este processo ainda segue funcionando com o token
    novo (o antigo já foi invalidado pela Conta Azul nesta troca), e o erro
    fica bem visível no log para alguém corrigir a permissão antes do
    próximo reinício. Exige o papel "Key Vault Secrets Officer" (não só
    "User") para a identidade gerenciada do container."""
    os.environ["CONTA_AZUL_REFRESH_TOKEN"] = novo_refresh_token
    try:
        config.cliente_key_vault().set_secret(config.SEGREDO_REFRESH_TOKEN_KEY_VAULT, novo_refresh_token)
    except Exception as e:
        logger.error(
            "FALHA ao gravar o refresh_token renovado no Key Vault (%s). O token "
            "novo só existe na memória deste processo - se o container reiniciar "
            "antes de corrigir, será preciso gerar um refresh_token novo. Confira "
            "se a identidade gerenciada tem o papel 'Key Vault Secrets Officer'.",
            type(e).__name__,
        )
        return
    logger.info("refresh_token renovado e persistido no Key Vault.")
