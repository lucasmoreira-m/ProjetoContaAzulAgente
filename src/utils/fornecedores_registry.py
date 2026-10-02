"""
Registro local de fornecedores já confirmados (CNPJ -> contato/categoria/rateio
na Conta Azul).

POR QUE ISSO EXISTE (contexto de negócio):
Antes, todo documento passava por match aproximado (fuzzy) por NOME contra a
Conta Azul, do zero, a cada execução - sem memória entre documentos. Isso tem
dois problemas: (1) nome é ambíguo/varia de escrita entre documentos do mesmo
fornecedor; (2) um fornecedor que já foi revisado e aprovado por um humano
uma vez continuava exigindo revisão manual toda vez.

Este módulo implementa uma memória simples e segura: quando um lançamento é
criado (automaticamente, com match exato, OU após aprovação humana no
painel), a combinação CNPJ -> contato_id/categoria_id/regra_rateio é
persistida aqui. Da próxima vez que o MESMO CNPJ aparecer, o sistema
reconhece direto, sem depender de match aproximado por nome.

IMPORTANTE - isto NÃO reduz a segurança do fluxo:
- Um CNPJ só entra no registro depois de já ter passado pelo fluxo normal
  (match exato automático OU aprovação humana explícita no painel).
- Um CNPJ novo, nunca visto, continua caindo em revisão humana como sempre -
  "aprender" aqui significa "lembrar o que já foi confirmado", nunca
  "adivinhar" algo que ninguém confirmou.
- O arquivo é texto plano, versionável e auditável (dá para ver no git quem
  mudou o quê e quando, se for commitado - avalie se cabe no .gitignore
  dependendo da política de dados da empresa, já que ele associa CNPJ a
  contato interno).

CONCORRÊNCIA (2026-09-03, corrigido - antes era uma limitação conhecida):
protegido por um lock de arquivo (`filelock`, mesmo padrão já usado em
`conta_azul_auth.py` para o cache de access_token) ao redor de TODA leitura
e escrita - antes, só a escrita em si era atômica (arquivo temporário +
os.replace), mas nada impedia painel Streamlit + monitor_pasta.py (agora no
MESMO processo, ver src/interface/app.py) de fazerem leitura-modificação-
escrita intercaladas e um perder a gravação do outro (last write wins). O
lock garante que só um lado por vez lê+modifica+escreve o arquivo inteiro.

Formato do arquivo (fornecedores_registry.json), chave = CNPJ normalizado
(só dígitos):
{
  "12345678000190": {
    "nome": "Imobiliária Central Ltda",
    "contato_id": "uuid-do-contato-na-conta-azul",
    "contato_nome": "Imobiliária Central Ltda",
    "categoria_id": "uuid-da-categoria",
    "categoria_nome": "4.01.002. Aluguel",
    "origem": "aprovado_humano",   # ou "match_exato_automatico"
    "primeira_vez": "2026-08-26T14:30:00",
    "ultima_confirmacao": "2026-08-26T14:30:00",
    "vezes_usado": 3
  }
}
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout as FileLockTimeout

from src.core import config
from src.utils import backup_registros

logger = config.get_logger(__name__)

CAMINHO_REGISTRO = Path(config.REGISTRO_FORNECEDORES_PATH)
_TIMEOUT_LOCK_SEGUNDOS = 30


def _lock() -> FileLock:
    """Um lock por chamada - cada função pública deste módulo adquire o
    lock UMA vez e faz sua leitura+modificação+escrita inteira dentro do
    mesmo `with` (ver mesmo padrão em historico_centros_funcionarios.py).

    Caminho do lock derivado de CAMINHO_REGISTRO a cada chamada (não numa
    constante fixada na importação) - acompanha monkeypatch de
    CAMINHO_REGISTRO nos testes."""
    return FileLock(config.caminho_trava(CAMINHO_REGISTRO), timeout=_TIMEOUT_LOCK_SEGUNDOS)


def normalizar_cnpj(cnpj: str | None) -> Optional[str]:
    """Mantém só os dígitos, para comparar CNPJs escritos com ou sem
    pontuação (ex: '12.345.678/0001-90' e '12345678000190')."""
    if not cnpj:
        return None
    digitos = re.sub(r"\D", "", cnpj)
    if len(digitos) != 14:
        # Não é um CNPJ válido (14 dígitos) - não usamos como chave.
        return None
    return digitos


def _carregar() -> dict:
    if not CAMINHO_REGISTRO.exists():
        return {}
    try:
        with open(CAMINHO_REGISTRO, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Falha ao ler %s (%s) - tratando como registro vazio para não travar o fluxo.", CAMINHO_REGISTRO, e)
        return {}


def _salvar(registro: dict) -> None:
    """Escrita atômica (mesmo padrão usado em conta_azul_auth.py para o .env)."""
    diretorio = CAMINHO_REGISTRO.parent or Path(".")
    diretorio.mkdir(parents=True, exist_ok=True)
    fd, caminho_temp = tempfile.mkstemp(dir=diretorio, prefix=".fornecedores.", suffix=".tmp")
    import os
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(registro, f, indent=2, ensure_ascii=False)
        os.replace(caminho_temp, CAMINHO_REGISTRO)
    except Exception:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)
        raise
    # Backup automático com rotação (2026-09-04, pedido explícito do
    # usuário) - ver backup_registros.py para o motivo. Só roda depois do
    # os.replace acima ter sucesso (o dado principal já está salvo).
    backup_registros.fazer_backup_com_rotacao(CAMINHO_REGISTRO)


def buscar_por_cnpj(cnpj: str | None) -> Optional[dict]:
    """Retorna o registro conhecido para este CNPJ, ou None se nunca visto
    (ou nunca confirmado) antes - também None se o lock não foi obtido a
    tempo (ver _lock): conservador, cai no fluxo normal de match por nome."""
    cnpj_norm = normalizar_cnpj(cnpj)
    if not cnpj_norm:
        return None
    try:
        with _lock():
            return _carregar().get(cnpj_norm)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para consultar CNPJ %s - seguindo sem o dado "
            "(cai no fluxo normal de match por nome).", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, cnpj_norm,
        )
        return None


def registrar_banco_pagamento_sem_boleto(
    cnpj: str | None,
    banco: str,
    origem: str = "aprovado_humano",
) -> None:
    """Grava (ou atualiza) qual conta financeira ('alfa' ou 'beta') este
    fornecedor usa para pagamento SEM boleto (NFS-e) - ver
    agente_nfs_e.resolver_banco_e_vencimento e regras_nfse.py para o
    porquê disso não ser uma característica óbvia/fixa do fornecedor.

    Reaproveita a MESMA entrada por CNPJ que já guarda contato/categoria -
    não cria um registro/arquivo novo. Só grava se o fornecedor já existir
    no registro (chamado sempre DEPOIS de registrar_confirmado, nunca
    antes) - se não existir ainda, cria uma entrada mínima só com o
    banco, para não perder a informação quando `registrar_confirmado`
    ainda não rodou nesta mesma chamada (ex: Camada 3, aprovação manual no
    painel que só confirma o banco, sem contato/categoria).

    `origem`: "aprovado_humano" (escolhido manualmente no painel na
    primeira vez que este fornecedor aparece sem boleto) ou
    "inferido_do_erp" (3+ lançamentos históricos na mesma conta
    financeira - ver agente_nfs_e._tentar_inferir_banco_do_erp)."""
    if banco not in ("alfa", "beta"):
        logger.error("Banco inválido '%s' para CNPJ %s - esperado 'alfa' ou 'beta'. Não gravado.", banco, cnpj)
        return

    cnpj_norm = normalizar_cnpj(cnpj)
    if not cnpj_norm:
        logger.debug("Sem CNPJ válido - não é possível registrar banco de pagamento sem boleto.")
        return

    try:
        with _lock():
            registro = _carregar()
            agora = datetime.now(timezone.utc).isoformat()
            existente = registro.get(cnpj_norm) or {"nome": None, "contato_id": None, "contato_nome": None}
            existente["banco_pagamento_sem_boleto"] = banco
            existente["origem_banco"] = origem
            existente["banco_confirmado_em"] = agora
            # Toda CONFIRMAÇÃO nova (humana ou por evidência forte do ERP)
            # zera o contador de reconfirmação periódica (ver
            # regras_nfse.LIMITE_USOS_AUTONOMOS_BANCO_SEM_RECONFIRMAR) -
            # renova a confiança por mais N usos autônomos.
            existente["usos_autonomos_desde_confirmacao"] = 0
            registro[cnpj_norm] = existente
            _salvar(registro)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss - banco de pagamento sem boleto do CNPJ %s "
            "NÃO foi salvo desta vez.", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, cnpj_norm,
        )
        return
    logger.info("Banco de pagamento sem boleto do CNPJ %s registrado: %s (origem=%s).", cnpj_norm, banco, origem)


def incrementar_uso_autonomo_banco(cnpj: str | None) -> int:
    """Chame isto depois de CADA lançamento automático (Camada 1, banco já
    aprendido) que efetivamente saiu sem revisão humana - conta quantos
    usos seguidos o banco aprendido sustentou sem reconfirmação nova (ver
    regras_nfse.LIMITE_USOS_AUTONOMOS_BANCO_SEM_RECONFIRMAR). Devolve a
    contagem nova (0 se o CNPJ não tiver registro/banco aprendido - nada a
    incrementar)."""
    cnpj_norm = normalizar_cnpj(cnpj)
    if not cnpj_norm:
        return 0
    try:
        with _lock():
            registro = _carregar()
            existente = registro.get(cnpj_norm)
            if not existente or not existente.get("banco_pagamento_sem_boleto"):
                return 0
            nova_contagem = existente.get("usos_autonomos_desde_confirmacao", 0) + 1
            existente["usos_autonomos_desde_confirmacao"] = nova_contagem
            registro[cnpj_norm] = existente
            _salvar(registro)
            return nova_contagem
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss - contagem de uso autônomo do banco do CNPJ %s "
            "NÃO foi incrementada desta vez (o lançamento em si não foi afetado).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, cnpj_norm,
        )
        return 0


def registrar_confirmado(
    cnpj: str | None,
    nome: str,
    contato_id: str,
    contato_nome: str,
    categoria_id: str | None = None,
    categoria_nome: str | None = None,
    origem: str = "aprovado_humano",
    rateio: list[dict] | None = None,
) -> None:
    """Grava (ou atualiza) uma combinação CONFIRMADA de fornecedor. Chame
    isto depois que:
    - um lançamento já foi criado com sucesso (automaticamente com match
      exato, ou após aprovação humana explícita no painel) - `origem`
      "match_exato_automatico" ou "aprovado_humano"; ou
    - um padrão de repetição foi identificado direto no histórico da
      Conta Azul (Camada 2 do Funil de Decisão Contábil, ver
      agente_integrado._tentar_inferir_padrao_do_erp) - `origem`
      "inferido_do_erp" - AINDA sem nenhum lançamento novo criado por
      este agente, mas com evidência de 3+ lançamentos passados reais
      usando a mesma categoria.

    `rateio`: divisão por centro de custo CONFIRMADA para este fornecedor,
    em PERCENTUAL (mesmo formato de regras_rateio.REGRAS_RATEIO - lista de
    {centro_custo_id, centro_custo_nome, percentual}, sem "valor" - o
    valor de cada documento futuro é diferente, então o que se aprende é
    a PROPORÇÃO, recalculada a cada lançamento via
    regras_rateio.calcular_rateio). Opcional - None quando o fornecedor
    não tem rateio (paga tudo numa categoria só, sem centro de custo) ou
    quando não foi possível inferir com confiança (ex: histórico usa mais
    de um centro de custo sem um padrão claro de divisão)."""
    cnpj_norm = normalizar_cnpj(cnpj)
    if not cnpj_norm:
        # Sem CNPJ válido no documento, não há chave confiável para
        # aprender - o próximo documento desse fornecedor volta a passar
        # pelo fluxo normal de match por nome. Isso é esperado e seguro.
        logger.debug("Sem CNPJ válido para '%s' - não é possível registrar para aprendizado.", nome)
        return

    try:
        with _lock():
            registro = _carregar()
            agora = datetime.now(timezone.utc).isoformat()
            existente = registro.get(cnpj_norm)

            registro[cnpj_norm] = {
                "nome": nome,
                "contato_id": contato_id,
                "contato_nome": contato_nome,
                "categoria_id": categoria_id or (existente or {}).get("categoria_id"),
                "categoria_nome": categoria_nome or (existente or {}).get("categoria_nome"),
                "rateio_aprendido": rateio if rateio is not None else (existente or {}).get("rateio_aprendido"),
                "origem": origem,
                "primeira_vez": (existente or {}).get("primeira_vez", agora),
                "ultima_confirmacao": agora,
                "vezes_usado": (existente or {}).get("vezes_usado", 0) + 1,
                # Preserva o banco de pagamento sem boleto (NFS-e, ver
                # registrar_banco_pagamento_sem_boleto acima) - este método
                # não sabe nada sobre NFS-e, só não pode APAGAR o que já
                # foi aprendido por aquele fluxo.
                "banco_pagamento_sem_boleto": (existente or {}).get("banco_pagamento_sem_boleto"),
                "origem_banco": (existente or {}).get("origem_banco"),
                "banco_confirmado_em": (existente or {}).get("banco_confirmado_em"),
                "usos_autonomos_desde_confirmacao": (existente or {}).get("usos_autonomos_desde_confirmacao", 0),
            }
            _salvar(registro)
    except FileLockTimeout:
        # Não lança - o lançamento em si já foi criado com sucesso antes
        # desta chamada; perder só o registro de aprendizado não pode
        # travar o fluxo.
        logger.error(
            "Não foi possível obter o lock de %s em %ss - aprendizado do fornecedor CNPJ %s ('%s') NÃO "
            "foi salvo desta vez (o lançamento em si não foi afetado).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, cnpj_norm, nome,
        )
        return
    logger.info("Fornecedor CNPJ %s ('%s') registrado/atualizado no aprendizado local.", cnpj_norm, nome)