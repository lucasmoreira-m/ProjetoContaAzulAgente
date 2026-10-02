"""
Detecção de duplicidade - resolve o incidente real observado em uso:
o mesmo documento (IRPJ, folha de pagamento) foi reprocessado várias vezes
durante testes e criou lançamentos duplicados de verdade na Conta Azul,
porque o sistema nunca teve nenhuma proteção contra "isso já foi lançado
antes?".

DUAS CAMADAS, propositalmente independentes uma da outra:

CAMADA 1 - hash do arquivo (rápida, local, evita gastar cota do Gemini):
Detecta se o MESMO arquivo (byte a byte) já foi processado com sucesso
antes. Cobre o caso mais comum na prática: o mesmo PDF sendo solto de novo
em entrada/ (de propósito, em teste, ou por engano). Roda ANTES de
qualquer chamada ao Gemini - documento identificado como já processado
nem chega a gastar cota.

CAMADA 2 - checagem ao vivo na Conta Azul (mais lenta, mas a fonte de
verdade): antes de criar QUALQUER lançamento, confere se já existe algo
com a mesma categoria + contato + valor + vencimento na Conta Azul de
verdade. Cobre o caso onde o arquivo é diferente (outra extração, outro
scan) mas representa a MESMA cobrança - e também funciona mesmo que o
registro local (Camada 1) tenha sido perdido/apagado, porque consulta a
fonte real, não um cache.

Nenhuma das duas camadas decide sozinha "não lança" de forma silenciosa -
as duas SINALIZAM para revisão humana em vez de bloquear ou lançar sem
avisar. Já vimos hoje que decisão financeira automática sem checagem dá
errado; a resposta não é trocar por "bloqueio automático sem checagem"
(que erraria na direção oposta, escondendo lançamentos legítimos que só
coincidem em valor/data). Fica sempre com um humano a palavra final.

CONCORRÊNCIA (2026-09-03, corrigido) - CAMADA 1 apenas (a Camada 2 consulta
a Conta Azul ao vivo, sem estado local, não precisa de lock): o registro de
hashes (`arquivos_processados_registry.json`) é protegido por um lock de
arquivo (`filelock`, mesmo padrão de `conta_azul_auth.py`) ao redor de toda
leitura e escrita - antes, só a escrita em si era atômica, mas nada
impedia painel Streamlit + monitor_pasta.py (agora no MESMO processo, ver
src/interface/app.py) de perderem a gravação um do outro (last write
wins). Se o lock não for obtido a tempo, a checagem trata o arquivo como
"nunca visto" (mesma direção conservadora já usada para JSON corrompido) -
a Camada 2 continua sendo a rede de segurança real, que não depende deste
arquivo.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout as FileLockTimeout

from src.core import config
from src.core import conta_azul_client as ca

logger = config.get_logger(__name__)

CAMINHO_REGISTRO_ARQUIVOS = Path(
    os.getenv("REGISTRO_ARQUIVOS_PROCESSADOS_PATH", "arquivos_processados_registry.json")
)
_TIMEOUT_LOCK_SEGUNDOS = 30


def _lock() -> FileLock:
    """Um lock por chamada - cada função pública que toca o registro de
    hashes adquire o lock UMA vez e faz sua leitura+modificação+escrita
    inteira dentro do mesmo `with` (mesmo padrão de fornecedores_registry.py
    e historico_centros_funcionarios.py).

    Caminho do lock derivado de CAMINHO_REGISTRO_ARQUIVOS a cada chamada
    (não numa constante fixada na importação) - acompanha monkeypatch nos
    testes."""
    return FileLock(config.caminho_trava(CAMINHO_REGISTRO_ARQUIVOS), timeout=_TIMEOUT_LOCK_SEGUNDOS)

# Tolerância de valor ao comparar com lançamentos existentes na Conta Azul -
# absorve diferença de centavos de arredondamento, igual usamos no rateio.
TOLERANCIA_VALOR_DUPLICATA = 0.05


def calcular_hash_arquivo(caminho: str) -> str:
    """SHA-256 do conteúdo do arquivo, para detectar 'é literalmente o
    mesmo arquivo' independente do nome (que pode mudar com timestamp)."""
    sha256 = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(65536), b""):
            sha256.update(bloco)
    return sha256.hexdigest()


def _carregar_registro_arquivos() -> dict:
    if not CAMINHO_REGISTRO_ARQUIVOS.exists():
        return {}
    try:
        with open(CAMINHO_REGISTRO_ARQUIVOS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Registro de arquivos processados corrompido/ilegível (%s) - tratando como vazio.", e)
        return {}


def _salvar_registro_arquivos(registro: dict) -> None:
    diretorio = CAMINHO_REGISTRO_ARQUIVOS.parent or Path(".")
    diretorio.mkdir(parents=True, exist_ok=True)
    fd, caminho_temp = tempfile.mkstemp(dir=diretorio, prefix=".arquivos_proc.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(registro, f, indent=2, ensure_ascii=False)
        os.replace(caminho_temp, CAMINHO_REGISTRO_ARQUIVOS)
    except Exception:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)
        raise


def verificar_arquivo_ja_processado(caminho: str) -> Optional[dict]:
    """Camada 1. Retorna o registro anterior se este EXATO arquivo (mesmo
    conteúdo byte a byte) já foi processado antes, ou None se é a
    primeira vez que este conteúdo aparece (também None se o lock não foi
    obtido a tempo - ver _lock; a Camada 2, ao vivo na Conta Azul, continua
    valendo independente disso)."""
    hash_atual = calcular_hash_arquivo(caminho)
    try:
        with _lock():
            return _carregar_registro_arquivos().get(hash_atual)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para checar '%s' - tratando como 'nunca "
            "processado' desta vez (Camada 2, ao vivo na Conta Azul, continua protegendo).",
            CAMINHO_REGISTRO_ARQUIVOS, _TIMEOUT_LOCK_SEGUNDOS, os.path.basename(caminho),
        )
        return None


def registrar_arquivo_processado(caminho: str, resultado: dict) -> None:
    """Grava o resultado do processamento deste arquivo, associado ao seu
    hash - para a próxima vez que o MESMO conteúdo aparecer.

    Extrai o(s) protocolo(s) de duas formas possíveis de "detalhes":
    documento comum (um único dict com "protocolo") ou folha de pagamento
    (dict de resultados por tabela, cada um com seu próprio protocolo)."""
    hash_atual = calcular_hash_arquivo(caminho)

    detalhes = resultado.get("detalhes") or {}
    protocolo = detalhes.get("protocolo")
    if protocolo is None and isinstance(detalhes, dict):
        # Formato de folha de pagamento: dict de resultados por tabela.
        protocolos_tabelas = [
            v.get("detalhes", {}).get("protocolo")
            for v in detalhes.values()
            if isinstance(v, dict) and v.get("detalhes", {}).get("protocolo")
        ]
        if protocolos_tabelas:
            protocolo = protocolos_tabelas

    try:
        with _lock():
            registro = _carregar_registro_arquivos()
            registro[hash_atual] = {
                "nome_arquivo_original": os.path.basename(caminho),
                "sucesso": resultado.get("sucesso"),
                "motivo": resultado.get("motivo"),
                "protocolo": protocolo,
                "processado_em": datetime.now(timezone.utc).isoformat(),
            }
            _salvar_registro_arquivos(registro)
    except FileLockTimeout:
        # Não lança - o processamento em si já terminou (sucesso ou não);
        # perder só o registro de hash não pode travar o monitor.
        logger.error(
            "Não foi possível obter o lock de %s em %ss - registro de '%s' NÃO foi salvo desta vez "
            "(risco: se o mesmo arquivo for solto de novo, a Camada 1 não vai reconhecê-lo - a Camada 2 "
            "continua protegendo).", CAMINHO_REGISTRO_ARQUIVOS, _TIMEOUT_LOCK_SEGUNDOS, os.path.basename(caminho),
        )


def _normalizar_simples(texto: str | None) -> str:
    """Mesma lógica de normalização de `agente_integrado.normalizar` (sem
    acento, minúsculo) - reimplementada aqui (não importada) para evitar
    import circular: `agente_integrado.py` já importa este módulo."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("utf-8")
    return texto.lower().strip()


def verificar_lancamento_similar_existe(
    access_token: str,
    valor_total: float,
    data_vencimento_iso: str,
    categoria_nome: str | None = None,
    contato_nome: str | None = None,
    categoria_id: str | None = None,
    contato_id: str | None = None,
) -> Optional[dict]:
    """Camada 2. Consulta a Conta Azul DE VERDADE (não um cache local) por
    contas a pagar com o mesmo valor, vencimento, contato E categoria - a
    checagem mais confiável, porque não depende de nenhum estado local que
    possa se perder ou ficar desatualizado.

    CORREÇÃO (2026-09-01): `categoria_nome`/`contato_nome` já existiam como
    parâmetros (e a docstring desta função sempre prometeu "mesma categoria +
    contato"), mas o código não os usava - só valor+vencimento eram
    comparados. Isso causava falso-positivo real entre, por exemplo, dois
    funcionários diferentes com férias de mesmo valor vencendo no mesmo dia.
    Agora contato e categoria também precisam bater (por id, preferível e
    exato, ou por nome normalizado como fallback) para contar como duplicata.
    Quando nem id nem nome são informados para uma dimensão, essa dimensão
    não é exigida (comportamento permissivo, só por compatibilidade - todos
    os chamadores atuais já passam pelo menos o nome).

    data_vencimento_iso: formato AAAA-MM-DD (mesmo que converter_data produz).

    Retorna o lançamento encontrado (dict cru da API) se achar algo com
    valor, contato e categoria batendo (dentro da tolerância de valor) na
    mesma data de vencimento, ou None se não encontrar nada parecido."""
    try:
        resposta = ca.get(
            access_token,
            "/v1/financeiro/eventos-financeiros/contas-a-pagar/buscar",
            params={
                "data_vencimento_de": data_vencimento_iso,
                "data_vencimento_ate": data_vencimento_iso,
                "pagina": 1,
                "tamanho_pagina": 100,
            },
        )
    except Exception:
        # Uma falha na CHECAGEM de duplicata não deve impedir o fluxo
        # normal de decidir se lança ou manda para revisão - só loga o
        # aviso e segue sem a proteção desta camada específica (a Camada 1
        # continua valendo).
        logger.exception(
            "Falha ao consultar Conta Azul para checagem de duplicata (valor=%s, vencimento=%s) - "
            "seguindo sem essa checagem específica.", valor_total, data_vencimento_iso,
        )
        return None

    contato_nome_norm = _normalizar_simples(contato_nome)
    categoria_nome_norm = _normalizar_simples(categoria_nome)

    def _contato_bate(item: dict) -> bool:
        # CORREÇÃO (2026-09-01, incidente real): usar SÓ o id, com exclusão
        # mútua do nome, causou uma duplicata de verdade em produção - a
        # Conta Azul tinha DOIS cadastros de contato diferentes para a MESMA
        # pessoa ("Maria Exemplo Souza" com dois ids distintos), e o
        # lançamento antigo usava um id, o novo resolveu para o outro. Com
        # "só id" a checagem não bateu (ids diferentes) e lançou de novo -
        # pior que o comportamento anterior (só valor+vencimento), que teria
        # pego esse caso. Agora as duas fontes de sinal se complementam (OR,
        # não substituição): id igual OU nome normalizado igual já contam
        # como o mesmo contato - assim um cadastro duplicado no lado da
        # Conta Azul não vira um buraco na proteção contra duplicata.
        fornecedor_item = item.get("fornecedor") or {}
        if not contato_id and not contato_nome_norm:
            return True  # dimensão não informada pelo chamador - não exige match
        if contato_id and fornecedor_item.get("id") == contato_id:
            return True
        if contato_nome_norm and _normalizar_simples(fornecedor_item.get("nome")) == contato_nome_norm:
            return True
        return False

    def _categoria_bate(item: dict) -> bool:
        categorias_item = item.get("categorias") or []
        if not categoria_id and not categoria_nome_norm:
            return True  # dimensão não informada pelo chamador - não exige match
        if categoria_id and any(c.get("id") == categoria_id for c in categorias_item):
            return True
        if categoria_nome_norm and any(_normalizar_simples(c.get("nome")) == categoria_nome_norm for c in categorias_item):
            return True
        return False

    itens = resposta.get("itens") or resposta.get("items") or []
    for item in itens:
        valor_item = item.get("total") or item.get("valor") or 0
        try:
            valor_item = float(valor_item)
        except (TypeError, ValueError):
            continue
        if abs(valor_item - valor_total) <= TOLERANCIA_VALOR_DUPLICATA and _contato_bate(item) and _categoria_bate(item):
            logger.warning(
                "Possível duplicata: já existe lançamento com valor R$ %s em %s, contato/categoria "
                "batendo, na Conta Azul (id=%s, status=%s).",
                valor_item, data_vencimento_iso, item.get("id"), item.get("status"),
            )
            return item

    return None
