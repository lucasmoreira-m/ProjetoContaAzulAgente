"""
Registro local de centro de custo escolhido manualmente por funcionário
(nome normalizado -> centro de custo na Conta Azul).

POR QUE ISSO EXISTE (contexto de negócio):
Documentos individuais de férias/pró-labore às vezes não trazem centro de
custo marcado, e o histórico de lançamentos anteriores na Conta Azul também
pode não ter nada para aquele funcionário (ex: primeira férias dele desde
que o agente passou a operar). Nesses casos, o painel de revisão pede que um
humano escolha o centro de custo manualmente (ver agente_folha.py,
fonte_centro_custo="revisao_manual"). Este módulo grava essa escolha, para
uma camada de aprendizado futura poder consultá-la antes de cair em revisão
de novo para o mesmo funcionário.

IMPORTANTE - isto ainda NÃO é consultado automaticamente:
Por ora, `registrar_centro_custo_funcionario` só GRAVA a escolha feita no
painel; nenhum fluxo automático chama `buscar_centro_custo_funcionario`
ainda. Ligar isso à cascata de `agente_folha.buscar_centro_custo_historico_funcionario`
fica para uma tarefa futura, deliberadamente fora do escopo desta.

CONCORRÊNCIA (2026-09-03, corrigido): protegido por um lock de arquivo
(`filelock`, mesmo padrão já usado em `conta_azul_auth.py` para o cache de
access_token) ao redor de TODA leitura e escrita - antes, só a escrita em
si era atômica (arquivo temporário + os.replace), mas nada impedia duas
gravações concorrentes (ex: a thread do monitor de pastas gravando
aprendizado de centro de custo, ao mesmo tempo que um humano aprova algo
no painel Streamlit - agora rodando no MESMO processo, ver
src/interface/app.py) de fazerem leitura-modificação-escrita intercaladas
e uma perder a gravação da outra (last write wins). O lock garante que só
um lado por vez lê+modifica+escreve o arquivo inteiro.

Formato do arquivo (historico_centros_funcionarios.json), chave = nome do
funcionário normalizado (sem acento, minúsculo):
{
  "maria exemplo souza": {
    "nome_funcionario": "Maria Exemplo Souza",
    "centro_custo_id": "uuid-do-centro-na-conta-azul",
    "centro_custo_nome": "1122 - Administrativo",
    "primeira_vez": "2026-09-01T08:30:00+00:00",
    "ultima_confirmacao": "2026-09-01T08:30:00+00:00",
    "vezes_usado": 1
  }
}

CONTADOR DE MATURAÇÃO POR PADRÃO DE DOCUMENTO (2026-09-02):
Mesmo arquivo, chave reservada "_padroes_documento" (nunca colide com nome
de funcionário normalizado) - guarda, por padrão de documento (ex:
"rescisao", ver agente_folha.padrao_documento_para_maturacao), quantas
vezes um humano já aprovou manualmente esse padrão no painel, e as notas
de diretriz que ele deixou. Usado pela autonomia condicional: um padrão só
lança 100% automático depois de "maduro" (aprovado manualmente algumas
vezes) - ver agente_folha.documento_maduro_para_autonomia.
{
  "_padroes_documento": {
    "rescisao": {
      "padrao": "rescisao",
      "contagem_aprovacoes": 3,
      "notas_diretriz_humana": [
        {"texto": "Sempre conferir se a data bate com o TRCT", "data": "2026-09-01T08:30:00+00:00"}
      ],
      "primeira_aprovacao": "2026-08-20T08:30:00+00:00",
      "ultima_aprovacao": "2026-09-02T08:30:00+00:00"
    }
  }
}

DIRETRIZES GLOBAIS DE TEXTO (2026-09-03, refinamento pedido pelo usuário):
Mesmo arquivo, chave reservada "_diretrizes_globais_texto" (nunca colide
com nome de funcionário nem com "_padroes_documento") - regras GLOBAIS por
PALAVRA-CHAVE, não por funcionário nem por padrão de documento: "voo"/
"passagem" sempre sugere a mesma categoria, para QUALQUER colaborador que
viajar, por exemplo. Usado por `agente_integrado.py` para enriquecer o
prompt de extração de prestação de contas/reembolso (a IA recebe a lista e
é instruída a aplicar essas regras se encontrar as palavras no texto) e
por `painel_revisao.py` para pré-preencher os dropdowns de categoria/conta
financeira/centro de custo e para gravar/reforçar a regra quando um humano
aprova (ou digita uma dica nova).

"origem" distingue DUAS proveniências (2026-09-03,
`src/utils/semear_diretrizes_historico.py`): "aprovado_humano" (uma
aprovação real no painel) ou "inferido_do_historico" (extraído
estatisticamente do histórico real de lançamentos na Conta Azul, sem
revisão humana linha a linha - só usado quando o padrão é forte e
consistente entre vários fornecedores diferentes). "aprovado_humano" NUNCA
regride para "inferido_do_historico", mesmo que a semeadura seja
re-executada depois - uma confirmação humana real sempre vale mais que
uma inferência estatística.
{
  "_diretrizes_globais_texto": {
    "voo": {
      "palavra_chave": "voo",
      "categoria_nome": "4.15.002. Viagens - A Recuperar",
      "conta_financeira_nome": "Banco Alfa - Matriz",
      "centro_custo_nome": null,
      "origem": "aprovado_humano",
      "vezes_confirmada": 4,
      "primeira_vez": "2026-08-20T08:30:00+00:00",
      "ultima_confirmacao": "2026-09-03T08:30:00+00:00"
    }
  }
}

ORIENTAÇÕES LIVRES ("orientar a IA sobre como lançar", 2026-09-15, pedido
explícito do usuário): Mesmo arquivo, chave reservada "_diretrizes_livres"
- texto livre digitado por um humano na tela de revisão (painel_revisao.py),
sem estrutura de palavra-chave nenhuma, separado POR TIPO de documento
("comum", "nfse" ou "folha") para não misturar orientação de um tipo no
prompt de outro. Diferente das "Notas de Diretriz Humana" antigas da folha
(_padroes_documento acima, que só ficavam gravadas sem nunca serem lidas de
volta), este texto É injetado a cada extração futura do mesmo tipo (ver
agente_integrado.extrair_dados_documento, agente_nfs_e.extrair_dados_nfse,
extrair_resumo_folha.extrair_dados_folha) - vale a partir do PRÓXIMO
documento processado, nunca do que está sendo aprovado agora (a extração
dele já aconteceu antes de chegar na revisão).
{
  "_diretrizes_livres": {
    "comum": [
      {"texto": "Boletos da 'Telecom Exemplo Fibra CNPJ ...01' são sempre da filial SP - usar o contato 'Telecom Exemplo SP', nunca 'Telecom Exemplo RJ'.", "data": "2026-09-15T08:30:00+00:00"}
    ],
    "nfse": [],
    "folha": []
  }
}
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout as FileLockTimeout

from src.core import config
from src.utils import backup_registros

logger = config.get_logger(__name__)

CAMINHO_REGISTRO = Path(config.HISTORICO_CENTROS_FUNCIONARIOS_PATH)
_TIMEOUT_LOCK_SEGUNDOS = 30


def _lock() -> FileLock:
    """Um lock por chamada (não reentrante entre chamadas aninhadas) -
    cada função pública deste módulo adquire o lock UMA vez e faz sua
    leitura+modificação+escrita inteira dentro do mesmo `with`, para
    nenhuma outra thread/processo poder intercalar uma gravação no meio.

    O caminho do lock é derivado de CAMINHO_REGISTRO A CADA CHAMADA (não
    numa constante fixada na importação) para sempre acompanhar o valor
    atual de CAMINHO_REGISTRO - importante para os testes, que fazem
    `monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "...")`."""
    return FileLock(config.caminho_trava(CAMINHO_REGISTRO), timeout=_TIMEOUT_LOCK_SEGUNDOS)


def _normalizar_nome(nome: str | None) -> Optional[str]:
    if not nome:
        return None
    nome = unicodedata.normalize("NFKD", nome).encode("ASCII", "ignore").decode("utf-8")
    return nome.lower().strip()


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
    """Escrita atômica (mesmo padrão usado em fornecedores_registry.py)."""
    diretorio = CAMINHO_REGISTRO.parent or Path(".")
    diretorio.mkdir(parents=True, exist_ok=True)
    fd, caminho_temp = tempfile.mkstemp(dir=diretorio, prefix=".historico_centros.", suffix=".tmp")
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


def buscar_centro_custo_funcionario(nome_funcionario: str | None) -> Optional[dict]:
    """Retorna o registro conhecido para este funcionário, ou None se nunca
    confirmado antes (ou se o lock não foi obtido a tempo - ver _lock).
    Não é chamado por nenhum fluxo automático ainda."""
    nome_norm = _normalizar_nome(nome_funcionario)
    if not nome_norm:
        return None
    try:
        with _lock():
            return _carregar().get(nome_norm)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para consultar '%s' - seguindo sem o dado "
            "(cai em revisão manual normalmente).", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, nome_funcionario,
        )
        return None


def registrar_centro_custo_funcionario(nome_funcionario: str, centro_custo_id: str, centro_custo_nome: str) -> None:
    """Grava (ou atualiza) o centro de custo confirmado para este
    funcionário. Chame isto só depois que o lançamento já foi criado com
    sucesso, com o centro de custo escolhido manualmente no painel."""
    nome_norm = _normalizar_nome(nome_funcionario)
    if not nome_norm:
        logger.debug("Sem nome de funcionário válido - não é possível registrar para aprendizado.")
        return

    try:
        with _lock():
            registro = _carregar()
            agora = datetime.now(timezone.utc).isoformat()
            existente = registro.get(nome_norm)

            registro[nome_norm] = {
                "nome_funcionario": nome_funcionario,
                "centro_custo_id": centro_custo_id,
                "centro_custo_nome": centro_custo_nome,
                "primeira_vez": (existente or {}).get("primeira_vez", agora),
                "ultima_confirmacao": agora,
                "vezes_usado": (existente or {}).get("vezes_usado", 0) + 1,
            }
            _salvar(registro)
    except FileLockTimeout:
        # Nunca lança - perder este registro de aprendizado é ruim, mas não
        # pode travar um lançamento que já foi criado com sucesso.
        logger.error(
            "Não foi possível obter o lock de %s em %ss - aprendizado de centro de custo de '%s' NÃO foi "
            "salvo desta vez (o lançamento em si não foi afetado).", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, nome_funcionario,
        )
        return
    logger.info("Centro de custo de '%s' registrado/atualizado no aprendizado local: %s.", nome_funcionario, centro_custo_nome)


_CHAVE_PADROES_DOCUMENTO = "_padroes_documento"


def buscar_padrao_documento(padrao_documento: str | None) -> Optional[dict]:
    """Retorna o registro de maturação conhecido para este padrão de
    documento (ver agente_folha.padrao_documento_para_maturacao), ou None
    se ele nunca foi aprovado manualmente ainda (ou se o lock não foi
    obtido a tempo - ver _lock)."""
    padrao_norm = _normalizar_nome(padrao_documento)
    if not padrao_norm:
        return None
    try:
        with _lock():
            return _carregar().get(_CHAVE_PADROES_DOCUMENTO, {}).get(padrao_norm)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para consultar o padrão '%s' - tratando como "
            "'nunca aprovado' desta vez (conservador: cai em revisão manual em vez de autonomia total).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, padrao_documento,
        )
        return None


def contagem_aprovacoes_padrao(padrao_documento: str | None) -> int:
    """Quantas vezes um humano já aprovou manualmente este padrão de
    documento no painel - 0 se nunca (padrão novo, ou nunca aprovado)."""
    return (buscar_padrao_documento(padrao_documento) or {}).get("contagem_aprovacoes", 0)


# MATURAÇÃO COM CONTADOR (movido para cá em 2026-09-04 - antes vivia só em
# agente_folha.py; agente_integrado.py também precisa da mesma checagem
# para o Funil de Decisão Contábil em 3 Camadas de documento genérico, e
# os dois módulos não podem importar um do outro sem criar import
# circular - agente_folha.py já importa DE agente_integrado.py. Este
# módulo (historico_centros_funcionarios.py) não importa nenhum dos dois,
# então é o lugar neutro certo para a constante/checagem compartilhada.
# `agente_folha.documento_maduro_para_autonomia` agora só delega pra
# `padrao_esta_maduro` abaixo - mantido lá por compatibilidade com quem já
# chama por esse nome.
MATURACAO_MINIMA_PARA_AUTONOMIA = 3


def padrao_esta_maduro(padrao_documento: str | None) -> bool:
    """True só quando este padrão de documento (folha: ver
    agente_folha.padrao_documento_para_maturacao; genérico: uma chave
    como "cnpj:<numero>", ver agente_integrado.py) já foi aprovado
    manualmente pelo menos MATURACAO_MINIMA_PARA_AUTONOMIA vezes no
    painel. Autonomia condicional: mesmo que o robô resolva tudo sozinho,
    só pula a revisão humana quando o padrão já provou, algumas vezes,
    que a automação está certa."""
    return contagem_aprovacoes_padrao(padrao_documento) >= MATURACAO_MINIMA_PARA_AUTONOMIA


def registrar_aprovacao_padrao_documento(padrao_documento: str, nota_diretriz_humana: str | None = None) -> dict:
    """Chame isto sempre que um humano clicar em "Aprovar e Lançar" com
    SUCESSO no painel para um documento deste padrão - incrementa o
    contador de maturação (contagem_aprovacoes) e, se a pessoa preencheu o
    campo "📝 Notas de Diretriz Humana", acrescenta essa nota à lista
    (nunca sobrescreve notas anteriores). Devolve o registro atualizado
    (nunca None) - se `padrao_documento` vier vazio, devolve {} sem gravar
    nada, sem lançar exceção (não deve travar uma aprovação bem-sucedida
    por falta desse dado)."""
    padrao_norm = _normalizar_nome(padrao_documento)
    if not padrao_norm:
        logger.debug("Padrão de documento vazio - não é possível registrar aprovação para maturação.")
        return {}

    try:
        with _lock():
            registro = _carregar()
            padroes = registro.setdefault(_CHAVE_PADROES_DOCUMENTO, {})
            agora = datetime.now(timezone.utc).isoformat()
            existente = padroes.get(padrao_norm) or {}

            notas = list(existente.get("notas_diretriz_humana") or [])
            if nota_diretriz_humana and nota_diretriz_humana.strip():
                notas.append({"texto": nota_diretriz_humana.strip(), "data": agora})

            entrada_atualizada = {
                "padrao": padrao_documento,
                "contagem_aprovacoes": existente.get("contagem_aprovacoes", 0) + 1,
                "notas_diretriz_humana": notas,
                "primeira_aprovacao": existente.get("primeira_aprovacao", agora),
                "ultima_aprovacao": agora,
            }
            padroes[padrao_norm] = entrada_atualizada
            _salvar(registro)
    except FileLockTimeout:
        # Não lança - a aprovação em si (o lançamento manual) já aconteceu
        # com sucesso antes desta chamada; perder só o incremento do
        # contador de maturação não pode travar o painel. Devolve {} ->
        # quem chamou (agente_folha.registrar_aprovacao_manual_padrao) vê
        # contagem 0, resultado conservador (não acelera autonomia total à
        # toa por causa de uma contagem que não foi persistida).
        logger.error(
            "Não foi possível obter o lock de %s em %ss - contagem de maturação do padrão '%s' NÃO foi "
            "incrementada desta vez (a aprovação/lançamento em si não foi afetado).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, padrao_documento,
        )
        return {}

    logger.info(
        "Aprovação humana registrada para o padrão de documento '%s' - contagem de maturação agora: %s.",
        padrao_documento, entrada_atualizada["contagem_aprovacoes"],
    )
    return entrada_atualizada


_CHAVE_DIRETRIZES_GLOBAIS_TEXTO = "_diretrizes_globais_texto"


def buscar_diretrizes_globais_texto() -> dict:
    """Devolve TODAS as diretrizes globais de texto conhecidas (chave =
    palavra-chave normalizada, valor = dict com categoria_nome/
    conta_financeira_nome/centro_custo_nome sugeridos) - {} se nenhuma foi
    confirmada ainda, ou se o lock não foi obtido a tempo (degrada para
    "nenhuma diretriz disponível" - a extração/revisão seguem normalmente,
    só sem esse reforço extra desta vez)."""
    try:
        with _lock():
            return dict(_carregar().get(_CHAVE_DIRETRIZES_GLOBAIS_TEXTO, {}))
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para ler as diretrizes globais de texto - "
            "seguindo sem elas desta vez.", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS,
        )
        return {}


def registrar_diretriz_global_texto(
    palavra_chave: str,
    categoria_nome: str | None = None,
    conta_financeira_nome: str | None = None,
    centro_custo_nome: str | None = None,
    origem: str = "aprovado_humano",
    incrementar_por: int = 1,
) -> dict:
    """Grava (ou reforça) uma diretriz GLOBAL por palavra-chave - vale para
    QUALQUER colaborador/documento futuro que mencionar essa palavra, não
    só para quem está sendo aprovado agora. Chame isto sempre que um
    humano aprovar (ou corrigir) um lançamento de prestação de contas/
    reembolso no painel, para cada palavra-chave que ele confirmou ser
    relevante (usa os valores padrão: origem="aprovado_humano",
    incrementar_por=1).

    origem/incrementar_por (2026-09-03, semeadura a partir do histórico
    real da Conta Azul): `src/utils/semear_diretrizes_historico.py` chama
    esta mesma função com origem="inferido_do_historico" e
    incrementar_por=<contagem real de ocorrências> para semear o
    dicionário sem esperar aprovações manuais repetidas - MAS uma vez que
    "aprovado_humano" for alcançado (por QUALQUER chamada, seed ou não),
    a origem NUNCA regride de volta para "inferido_do_historico" - uma
    confirmação humana real sempre vale mais que uma inferência
    estatística, mesmo que a semeadura seja re-executada depois.

    Nunca apaga um campo já preenchido antes só porque esta chamada não
    informou aquele campo - só sobrescreve categoria/conta/centro de custo
    quando vier um valor não-None (um humano pode confirmar só a categoria
    numa aprovação e só o centro de custo em outra, sem que uma apague a
    outra).

    Nunca lança exceção - se o lock não for obtido a tempo, loga erro e
    devolve {} sem persistir (perder um reforço de aprendizado não pode
    travar uma aprovação que já aconteceu com sucesso)."""
    palavra_norm = _normalizar_nome(palavra_chave)
    if not palavra_norm:
        logger.debug("Palavra-chave vazia - não é possível registrar diretriz global de texto.")
        return {}

    try:
        with _lock():
            registro = _carregar()
            diretrizes = registro.setdefault(_CHAVE_DIRETRIZES_GLOBAIS_TEXTO, {})
            agora = datetime.now(timezone.utc).isoformat()
            existente = diretrizes.get(palavra_norm) or {}

            origem_final = (
                "aprovado_humano"
                if "aprovado_humano" in (existente.get("origem"), origem)
                else origem
            )

            entrada_atualizada = {
                "palavra_chave": palavra_chave.strip(),
                "categoria_nome": categoria_nome or existente.get("categoria_nome"),
                "conta_financeira_nome": conta_financeira_nome or existente.get("conta_financeira_nome"),
                "centro_custo_nome": centro_custo_nome or existente.get("centro_custo_nome"),
                "origem": origem_final,
                "vezes_confirmada": existente.get("vezes_confirmada", 0) + incrementar_por,
                "primeira_vez": existente.get("primeira_vez", agora),
                "ultima_confirmacao": agora,
            }
            diretrizes[palavra_norm] = entrada_atualizada
            _salvar(registro)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss - diretriz global para '%s' NÃO foi salva "
            "desta vez (o lançamento em si não foi afetado).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, palavra_chave,
        )
        return {}

    logger.info(
        "Diretriz global de texto '%s' registrada/reforçada - categoria='%s', conta='%s', centro_custo='%s' "
        "(confirmada %s vez(es)).",
        palavra_chave, entrada_atualizada["categoria_nome"], entrada_atualizada["conta_financeira_nome"],
        entrada_atualizada["centro_custo_nome"], entrada_atualizada["vezes_confirmada"],
    )
    return entrada_atualizada


_CHAVE_DIRETRIZES_LIVRES = "_diretrizes_livres"
TIPOS_DOCUMENTO_DIRETRIZ_LIVRE = ("comum", "nfse", "folha")


def buscar_diretrizes_livres(tipo_documento: str) -> list[dict]:
    """Devolve as orientações livres em texto (ver docstring do módulo,
    seção "ORIENTAÇÕES LIVRES") que humanos já gravaram para este tipo de
    documento ("comum", "nfse" ou "folha") - carregado A CADA chamada (não
    cacheado), para uma orientação gravada há poucos segundos já valer para
    o próximo documento processado. [] se nenhuma foi gravada ainda, ou se
    o lock não foi obtido a tempo (degrada para "sem orientação extra desta
    vez" - a extração segue normalmente)."""
    try:
        with _lock():
            return list(_carregar().get(_CHAVE_DIRETRIZES_LIVRES, {}).get(tipo_documento, []))
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss para ler as orientações livres de '%s' - "
            "seguindo sem elas desta vez.", CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, tipo_documento,
        )
        return []


def registrar_diretriz_livre(tipo_documento: str, texto: str | None) -> None:
    """Grava uma orientação livre digitada por um humano na tela de revisão
    (ver painel_revisao.py) - passa a valer para todo PRÓXIMO documento
    deste mesmo tipo processado (nunca para o que está sendo aprovado
    agora). Não faz nada se `texto` vier vazio/só espaços.

    Não grava linha duplicada (mesmo texto já presente, comparado sem
    diferenciar maiúsculas/espaços nas pontas) - permite ao humano deixar o
    campo preenchido em várias aprovações seguidas sem empilhar repetições
    idênticas no prompt. Nunca lança exceção - se o lock não for obtido a
    tempo, loga erro e não persiste (não pode travar uma aprovação que já
    aconteceu com sucesso)."""
    texto_limpo = (texto or "").strip()
    if not texto_limpo:
        return
    try:
        with _lock():
            registro = _carregar()
            por_tipo = registro.setdefault(_CHAVE_DIRETRIZES_LIVRES, {})
            lista = por_tipo.setdefault(tipo_documento, [])
            if any((item.get("texto") or "").strip().lower() == texto_limpo.lower() for item in lista):
                return
            lista.append({"texto": texto_limpo, "data": datetime.now(timezone.utc).isoformat()})
            _salvar(registro)
    except FileLockTimeout:
        logger.error(
            "Não foi possível obter o lock de %s em %ss - orientação livre para '%s' NÃO foi salva "
            "desta vez (o lançamento em si não foi afetado).",
            CAMINHO_REGISTRO, _TIMEOUT_LOCK_SEGUNDOS, tipo_documento,
        )
        return
    logger.info("Orientação livre registrada para tipo '%s': %s", tipo_documento, texto_limpo)


def formatar_diretrizes_livres_para_prompt(diretrizes: list[dict]) -> str:
    """Renderiza a lista de orientações livres (ver buscar_diretrizes_livres)
    como uma lista com marcador, pronta para injetar no prompt de extração.
    Devolve uma frase clara quando não há nenhuma ainda - nunca inventa uma
    orientação que ninguém escreveu."""
    if not diretrizes:
        return "(nenhuma orientação humana registrada ainda - ignore esta seção.)"
    linhas = [f"- {d['texto']}" for d in diretrizes if d.get("texto")]
    return "\n" + "\n".join(linhas) if linhas else "(nenhuma orientação humana registrada ainda - ignore esta seção.)"
