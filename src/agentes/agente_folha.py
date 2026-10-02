"""
Regras de negócio para lançamentos derivados da folha de pagamento
(FGTS, INSS, IRRF, Salários, Bolsa de Estágio, Adiantamento Salarial).

CORREÇÃO ESTRUTURAL IMPORTANTE (ver relatório de auditoria, item crítico):
O arquivo original tinha praticamente TODO o seu conteúdo duplicado -
imports, `separar_e_ajustar_valores_negativos`, `validar_rateio_do_documento_folha`,
`resolver_conta_financeira`, `processar_tabela` e `processar_documento_folha`
apareciam DUAS VEZES seguidas no arquivo (provavelmente um erro de
copiar/colar). Como Python usa sempre a ÚLTIMA definição de cada nome no
módulo, o código "funcionava" silenciosamente usando a segunda cópia de
cada função - mas a ÚLTIMA definição de `processar_documento_folha`
(a que realmente rodava) tinha regredido: **não aceitava mais o parâmetro
`usuario`**, enquanto `monitor_pasta.py` a chamava com
`processar_documento_folha(dados_folha, usuario)` - 2 argumentos.

Ou seja: o pipeline de folha de pagamento estava quebrando com
`TypeError: processar_documento_folha() takes 1 positional argument but 2
were given` em toda execução real via `monitor_pasta.py`. Combinado com o
mesmo tipo de bug em `agente_integrado.processar_documento` (ver esse
arquivo), o `monitor_pasta.py` provavelmente não estava conseguindo
processar NENHUM documento, de nenhum dos dois tipos.

Esta versão mantém apenas UMA cópia de cada função (a versão mais completa
e correta, com suporte a `usuario`), sem alterar nenhuma regra de negócio.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
from datetime import date

from src.core import config
from src.core import conta_azul_client as ca
from src.utils import historico_centros_funcionarios
from src.utils import verificacao_duplicidade
from src.utils.formato_texto import (
    competencia_para_texto_br, data_para_iso, data_para_texto_br, parsear_competencia,
    parsear_data, texto_ou,
)
from src.agentes.agente_integrado import (
    buscar_categoria, buscar_contato, buscar_centro_custo, buscar_conta_financeira,
    criar_lancamento, normalizar,
)
from src.core.conta_azul_auth import obter_access_token
from src.agentes.extrair_resumo_folha import extrair_dados_folha
from src.utils.regras_folha_pagamento import CENTRO_CUSTO_PLACEHOLDER, REGRA_RESCISAO_FIXA, REGRAS_FOLHA

logger = config.get_logger(__name__)

TOLERANCIA_RATEIO_CENTAVOS = 0.05  # tolerância para diferenças de arredondamento vindas do próprio documento fonte

# Mapeia o nome do banco identificado na tabela (arquivo_bancario) para o nome
# real da conta financeira na Conta Azul. Usado apenas quando a regra do tipo
# de lançamento não tem uma conta financeira fixa (conta_financeira_nome=None),
# como é o caso de Salários, Bolsa de Estágio e Adiantamento Salarial.
ARQUIVO_BANCARIO_PARA_CONTA = {
    "alfa": "Banco Alfa - Matriz",
    "beta": "Banco Beta - Matriz",
}


def _descricao_tipo_sem_regra(tipo_lancamento, funcionario=None, competencia=None, data=None) -> str:
    """Descrição de fallback para tipos de tabela SEM regra fixa em
    REGRAS_FOLHA (ou para caminhos de erro): "<tipo> - <identificador>",
    onde o identificador é o funcionário, a competência ou a data do
    documento - o que existir, nessa ordem.

    CORREÇÃO (2026-09-14, caso real de produção): antes cada um desses
    pontos montava a f-string na mão com `dados_folha.get('competencia', '')`,
    e como o schema declara `competencia: Optional[str] = None`, a chave
    EXISTE com valor None - o default do `.get` não entrava, e a descrição
    ia parar na Conta Azul como "beneficios - None". Quando não há nenhum
    identificador, a descrição agora fica só com o tipo, sem o " - " solto
    no fim. A data entra sempre em DD/MM/AAAA (nunca o isoformat da API),
    e a competência em MM/AAAA."""
    identificador = (
        texto_ou(funcionario)
        or competencia_para_texto_br(competencia)
        or data_para_texto_br(data)
    )
    tipo = texto_ou(tipo_lancamento, "lancamento_folha")
    return f"{tipo} - {identificador}" if identificador else tipo

# Termos (já normalizados - sem acento, minúsculo) usados pela trava de
# segurança de GRRF em processar_tabela: qualquer um presente no texto
# bruto da tabela ou no nome do arquivo força tipo "fgts" -> "grrf".
_TERMOS_GRRF = ("grrf", "rescisao", "rescisorio", "quitacao")

# MATURAÇÃO COM CONTADOR (2026-09-02, pedido explícito do usuário): mesmo
# um padrão de documento com autonomia total implementada (ver
# _tentar_lancamento_automatico_rescisao) só lança 100% automático depois
# de já ter sido aprovado manualmente esse tanto de vezes no painel - dá
# tempo de um humano confirmar, algumas vezes, que a regra automática está
# certa antes de tirar a auditoria humana do caminho.
#
# Constante e checagem de limite agora vivem em
# historico_centros_funcionarios.py (2026-09-04) - reexportada aqui por
# compatibilidade com quem já importa `agente_folha.MATURACAO_MINIMA_
# PARA_AUTONOMIA` (ver docstring de lá para o motivo do módulo compartilhado).
MATURACAO_MINIMA_PARA_AUTONOMIA = historico_centros_funcionarios.MATURACAO_MINIMA_PARA_AUTONOMIA


def padrao_documento_para_maturacao(tipo_lancamento: str | None) -> str:
    """Nome CANÔNICO do "padrão de documento" usado pelo contador de
    maturação (registrar_aprovacao_manual_padrao/documento_maduro_para_
    autonomia). Agrupa variações de texto que a IA pode gerar para o mesmo
    tipo de situação (ex: "termo_rescisao", "rescisao_contrato_trabalho")
    sob uma única chave estável ("rescisao") - sem isso, pequenas
    diferenças de fraseado do "tipo" extraído fragmentariam a contagem
    entre várias chaves diferentes e o padrão nunca amadureceria de
    verdade. Tipos sem agrupamento especial caem no próprio nome
    normalizado (espaço para outros padrões de autonomia no futuro)."""
    tipo_norm = normalizar((tipo_lancamento or "").replace("_", " "))
    if "rescis" in tipo_norm:
        return "rescisao"
    return tipo_norm or "desconhecido"


def registrar_aprovacao_manual_padrao(tipo_lancamento: str | None, nota_diretriz_humana: str | None = None) -> int:
    """Chamado pelo painel de revisão (painel_revisao.py) sempre que um
    humano clica em "Aprovar e Lançar" com SUCESSO para uma tabela de
    folha - incrementa o contador de maturação do padrão correspondente
    (ver padrao_documento_para_maturacao) e grava a nota de diretriz
    humana, se preenchida. Devolve a nova contagem."""
    padrao = padrao_documento_para_maturacao(tipo_lancamento)
    entrada = historico_centros_funcionarios.registrar_aprovacao_padrao_documento(padrao, nota_diretriz_humana)
    return entrada.get("contagem_aprovacoes", 0)


def documento_maduro_para_autonomia(tipo_lancamento: str | None) -> bool:
    """True só quando o padrão de documento (ver padrao_documento_para_
    maturacao) já foi aprovado manualmente pelo menos MATURACAO_MINIMA_
    PARA_AUTONOMIA vezes. Autonomia condicional: mesmo que o robô resolva
    contato/categoria/conta/rateio/data sozinho, só pula a revisão humana
    quando o padrão já provou, algumas vezes, que a automação está certa."""
    padrao = padrao_documento_para_maturacao(tipo_lancamento)
    return historico_centros_funcionarios.padrao_esta_maduro(padrao)


def separar_e_ajustar_valores_negativos(rateio_documento: list[dict], valor_total: float):
    """Regra de negócio confirmada: quando um centro de custo aparece com valor
    zero ou negativo (ex: crédito/estorno de um departamento), ele é removido
    da lista de rateio, e o valor absoluto dele é somado ao total do lançamento
    (vira um complemento do valor a pagar, não uma linha própria no rateio).
    Itens com valor exatamente zero são removidos silenciosamente."""
    positivos = [item for item in rateio_documento if item["valor"] > 0]
    negativos = [item for item in rateio_documento if item["valor"] < 0]
    zerados = [item for item in rateio_documento if item["valor"] == 0]

    if not negativos and not zerados:
        return rateio_documento, valor_total, []

    soma_abs_negativos = round(sum(abs(item["valor"]) for item in negativos), 2)
    valor_total_ajustado = round(valor_total + soma_abs_negativos, 2)

    ajustes = [
        f"Centro '{item['centro_custo']}' tinha valor R$ {item['valor']} - removido do rateio "
        f"e R$ {abs(item['valor'])} somado ao total como complemento."
        for item in negativos
    ]

    return positivos, valor_total_ajustado, ajustes


def validar_rateio_do_documento_folha(access_token: str, rateio_documento: list[dict], valor_total: float):
    """Cada centro de custo mencionado precisa existir de fato (match exato) e
    a soma precisa bater com o valor total, com uma pequena tolerância de
    arredondamento vinda do próprio documento fonte.

    Retorna (rateio_pronto, erros, cadastros_faltando) - o terceiro item é
    estruturado (não texto livre), para o resumo de "cadastros pendentes" no
    painel. Só entra ali o centro de custo genuinamente NÃO encontrado -
    linha com valor zero/negativo não é problema de cadastro, é regra de
    negócio (ver separar_e_ajustar_valores_negativos)."""
    rateio_pronto = []
    erros = []
    cadastros_faltando = []

    for item in rateio_documento:
        if item["valor"] <= 0:
            erros.append(f"Centro de custo '{item['centro_custo']}' com valor zero ou negativo não tratado (R$ {item['valor']}).")
            continue

        centro, tipo_match = buscar_centro_custo(access_token, item["centro_custo"])
        if not centro or tipo_match != "exato":
            erros.append(f"Centro de custo '{item['centro_custo']}' do documento não encontrado (match exato) na Conta Azul.")
            cadastros_faltando.append({
                "tipo": "centro_custo",
                "nome_no_documento": item["centro_custo"],
                "sugestao_parecida": centro["nome"] if centro else None,
            })
            continue
        rateio_pronto.append({
            "centro_custo_id": centro["id"],
            "centro_custo_nome": centro["nome"],
            "percentual": None,
            "valor": round(item["valor"], 2),
        })

    if not erros:
        soma = round(sum(item["valor"] for item in rateio_pronto), 2)
        diferenca = round(soma - round(valor_total, 2), 2)
        if abs(diferenca) > TOLERANCIA_RATEIO_CENTAVOS:
            erros.append(
                f"Soma do rateio do documento (R$ {soma}) não bate com o valor total (R$ {valor_total}) "
                f"- diferença de R$ {diferenca}, maior que a tolerância de R$ {TOLERANCIA_RATEIO_CENTAVOS}."
            )
        elif diferenca != 0:
            rateio_pronto[-1]["valor"] = round(rateio_pronto[-1]["valor"] - diferenca, 2)
            logger.info("Diferença de R$ %s (arredondamento do documento) absorvida no último item do rateio.", diferenca)

    return rateio_pronto, erros, cadastros_faltando


def resolver_conta_financeira(access_token: str, regra: dict, tabela: dict):
    """Decide qual conta financeira usar: fixa da regra, ou dinâmica com base
    no banco identificado na própria tabela (arquivo_bancario). Retorna
    (conta_id, nome_usado, erro)."""
    if regra.get("conta_financeira_nome"):
        conta, tipo_match = buscar_conta_financeira(access_token, regra["conta_financeira_nome"])
        if not conta or tipo_match != "exato":
            return None, None, f"Conta financeira fixa '{regra['conta_financeira_nome']}' não encontrada na Conta Azul."
        return conta["id"], conta["nome"], None

    banco = (tabela.get("arquivo_bancario") or "").strip().lower()
    nome_conta_esperado = ARQUIVO_BANCARIO_PARA_CONTA.get(banco)
    if not nome_conta_esperado:
        return None, None, (
            f"Não foi possível determinar a conta financeira: a tabela não indicou um banco "
            f"reconhecido (veio '{tabela.get('arquivo_bancario')}'), e não há conta financeira fixa "
            f"para este tipo de lançamento."
        )

    conta, tipo_match = buscar_conta_financeira(access_token, nome_conta_esperado)
    if not conta or tipo_match != "exato":
        return None, None, f"Conta financeira '{nome_conta_esperado}' (para banco '{tabela.get('arquivo_bancario')}') não encontrada na Conta Azul."
    return conta["id"], conta["nome"], None


def buscar_centro_custo_historico_funcionario(
    access_token: str, nome_funcionario: str, meses_para_tras: int = 12
) -> tuple[dict | None, str | None]:
    """Busca, no histórico de contas a pagar da Conta Azul, o centro de
    custo mais recente usado para um funcionário específico - usado para
    ferias/pro_labore, que não têm centro de custo marcado no próprio
    documento (ao contrário das tabelas de FGTS/INSS, que têm uma linha
    de rateio explícita por centro de custo).

    Percorre mês a mês, do mais recente para o mais antigo - o endpoint
    de busca não aguenta ranges de vários anos numa chamada só (dá timeout
    de leitura; descoberto em 2026-08-31) - e para assim que encontra o
    primeiro lançamento anterior dessa pessoa.

    Retorna (centro_custo_dict, None) se achar, ou (None, motivo) se não
    encontrar nada nos meses pesquisados - nesse caso, o centro de custo
    fica pendente de escolha manual no painel."""
    nome_norm = normalizar(nome_funcionario)
    ano, mes = date.today().year, date.today().month

    for _ in range(meses_para_tras):
        primeiro_dia = date(ano, mes, 1)
        ultimo_dia = date(ano, mes, calendar.monthrange(ano, mes)[1])
        try:
            resposta = ca.get(
                access_token, "/v1/financeiro/eventos-financeiros/contas-a-pagar/buscar",
                params={
                    "data_vencimento_de": primeiro_dia.isoformat(),
                    "data_vencimento_ate": ultimo_dia.isoformat(),
                    "pagina": 1, "tamanho_pagina": 200,
                },
            )
            itens = resposta.get("itens") or resposta.get("items") or []
        except Exception:
            logger.exception(
                "Falha ao consultar histórico de %s/%s para '%s' - pulando este mês.",
                mes, ano, nome_funcionario,
            )
            itens = []

        candidatos = [
            it for it in itens
            if normalizar((it.get("fornecedor") or {}).get("nome") or "") == nome_norm
        ]
        if candidatos:
            candidatos.sort(key=lambda it: it.get("data_vencimento") or "", reverse=True)
            centros = candidatos[0].get("centros_de_custo") or []
            if centros:
                centro = centros[0]
                logger.info(
                    "Histórico encontrado para '%s': centro de custo '%s' (lançamento de %s/%s).",
                    nome_funcionario, centro.get("nome"), mes, ano,
                )
                return {"id": centro.get("id"), "nome": centro.get("nome")}, None

        if mes == 1:
            ano, mes = ano - 1, 12
        else:
            mes -= 1

    return None, (
        f"Nenhum lançamento anterior encontrado para '{nome_funcionario}' nos últimos "
        f"{meses_para_tras} meses - selecione o centro de custo manualmente."
    )


# Separador pode ser "-", "_" ou espaço - arquivos reais chegam nos três
# formatos (ex: "2026-08-26", "2026_07_20", "2026 08 31"). O grupo 2 (\2 no
# segundo separador) exige que os DOIS separadores de uma mesma data sejam
# iguais, para não casar algo tipo "2026-08_26" por acidente.
_REGEX_DATA_AAAA_MM_DD = re.compile(r"(\d{4})([-_ ])(\d{2})\2(\d{2})")
_REGEX_DATA_DUAS_PARTES_MAIS_ANO = re.compile(r"(\d{2})([-_ ])(\d{2})\2(\d{4})")


def extrair_data_do_nome_arquivo(caminho_documento: str | None) -> date | None:
    """Última tentativa de descobrir a data de um lançamento de rescisão/
    salário quando nem a competência do documento nem uma data individual
    (data_pagamento_documento) puderam ser lidas. Também é a FONTE PRIMÁRIA
    de data para tabelas de rescisão (ver processar_tabela) - tentada antes
    do conteúdo do documento nesse caso específico.

    Confirmado com caso real de produção: o nome do arquivo físico de um
    Termo de Rescisão traz a data de pagamento escrita (ex:
    "SALARIOS_SEMBANCO_0_2026-08-26.pdf") - essa data é usada DIRETAMENTE
    como vencimento/competência (sem passar por ultimo_dia_util_do_mes ou
    outra fórmula), porque já é a data exata, não uma competência a partir
    da qual calcular um vencimento.

    Aceita AAAA-MM-DD (tentado primeiro, inequívoco por causa do ano de 4
    dígitos) ou um par de números de 2 dígitos + ano, testado primeiro como
    DD-MM-AAAA (padrão brasileiro) e, só se essa leitura não formar uma
    data válida (ex: "08-25-2026", onde "25" não pode ser mês), como
    MM-DD-AAAA (formato americano - alguns arquivos chegam assim). Quando
    os dois números são <=12 a ambiguidade é real e não dá pra saber com
    certeza qual formato foi usado - assume-se brasileiro por padrão
    (empresa é brasileira).

    Nunca lança exceção - devolve None se o nome do arquivo não tiver uma
    data reconhecível ou válida, para o chamador poder cair em revisão manual
    normalmente em vez de quebrar."""
    if not caminho_documento:
        return None
    nome_arquivo = os.path.basename(caminho_documento)

    m = _REGEX_DATA_AAAA_MM_DD.search(nome_arquivo)
    if m:
        ano, _sep, mes, dia = m.groups()
        try:
            return date(int(ano), int(mes), int(dia))
        except ValueError as e:
            logger.warning(
                "Nome de arquivo '%s' parecia ter uma data AAAA-MM-DD, mas não é válida: %s", nome_arquivo, e,
            )

    m = _REGEX_DATA_DUAS_PARTES_MAIS_ANO.search(nome_arquivo)
    if m:
        primeiro, _sep, segundo, ano = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        try:
            return date(ano, segundo, primeiro)  # DD-MM-AAAA (padrão brasileiro, tentado primeiro)
        except ValueError:
            try:
                return date(ano, primeiro, segundo)  # MM-DD-AAAA (formato americano)
            except ValueError as e:
                logger.warning(
                    "Nome de arquivo '%s' parecia ter uma data, mas não é válida em DD-MM-AAAA nem MM-DD-AAAA: %s",
                    nome_arquivo, e,
                )

    return None


def buscar_regra_historico_por_tipo(
    access_token: str, tipo_lancamento: str, meses_para_tras: int = 12
) -> tuple[dict | None, str | None]:
    """"Cérebro de fallback" para um tipo de lançamento SEM regra cadastrada
    em REGRAS_FOLHA (ex: um "Termo de Rescisão" de um tipo/formato que a
    empresa ainda não tinha visto). Em vez de desistir direto e mandar tudo
    pra revisão manual sem nenhuma sugestão, busca no histórico de contas a
    pagar da Conta Azul algum lançamento anterior cuja DESCRIÇÃO contenha
    esse mesmo tipo (ex: a rescisão de outro funcionário, processada antes)
    e herda a CATEGORIA usada da última vez - a empresa já tem um padrão
    conhecido pra esse tipo de despesa, mesmo sem regra fixa cadastrada.

    NÃO herda conta financeira: o endpoint de busca não devolve esse campo
    nos itens (confirmado nesta sessão) - só categoria é recuperável daqui.
    Conta financeira continua como sugestão padrão (config.CONTA_FINANCEIRA_ID_PADRAO)
    no dados_para_edicao, editável no painel como qualquer outra sugestão.

    Mesma limitação de paginação por mês do buscar_centro_custo_historico_funcionario
    (o endpoint não aguenta um range de vários anos numa chamada só).

    Retorna ({"categoria": {"id":..., "nome":...}}, None) se achar, ou
    (None, motivo) se não achar nada - nunca lança exceção (é justamente o
    caminho de fallback para o caso desconhecido, não pode quebrar)."""
    try:
        # tipo_lancamento costuma ser um identificador em snake_case (ex:
        # "termo_rescisao"), enquanto a descrição real na Conta Azul é texto
        # natural com conectores (ex: "Termo de Rescisão - Fulano de Tal") -
        # substring literal quase nunca bate por causa do "_"/conectores.
        # Em vez disso, exige que TODAS as palavras (>2 letras) do tipo
        # apareçam em algum lugar da descrição normalizada, na ordem que for.
        palavras_tipo = [p for p in normalizar((tipo_lancamento or "").replace("_", " ")).split() if len(p) > 2]
        if not palavras_tipo:
            return None, "Tipo de lançamento vazio - não há o que buscar no histórico."

        ano, mes = date.today().year, date.today().month

        for _ in range(meses_para_tras):
            primeiro_dia = date(ano, mes, 1)
            ultimo_dia = date(ano, mes, calendar.monthrange(ano, mes)[1])
            try:
                resposta = ca.get(
                    access_token, "/v1/financeiro/eventos-financeiros/contas-a-pagar/buscar",
                    params={
                        "data_vencimento_de": primeiro_dia.isoformat(),
                        "data_vencimento_ate": ultimo_dia.isoformat(),
                        "pagina": 1, "tamanho_pagina": 200,
                    },
                )
                itens = resposta.get("itens") or resposta.get("items") or []
            except Exception:
                logger.exception(
                    "Falha ao consultar histórico de %s/%s para o tipo '%s' - pulando este mês.",
                    mes, ano, tipo_lancamento,
                )
                itens = []

            candidatos = [
                it for it in itens
                if all(palavra in normalizar(it.get("descricao") or "") for palavra in palavras_tipo)
            ]
            if candidatos:
                candidatos.sort(key=lambda it: it.get("data_vencimento") or "", reverse=True)
                categorias_item = candidatos[0].get("categorias") or []
                if categorias_item:
                    categoria_encontrada = categorias_item[0]
                    logger.info(
                        "Histórico encontrado para tipo '%s': categoria '%s' (lançamento de %s/%s).",
                        tipo_lancamento, categoria_encontrada.get("nome"), mes, ano,
                    )
                    return {
                        "categoria": {"id": categoria_encontrada.get("id"), "nome": categoria_encontrada.get("nome")},
                    }, None

            if mes == 1:
                ano, mes = ano - 1, 12
            else:
                mes -= 1

        return None, (
            f"Nenhum lançamento anterior encontrado para o tipo '{tipo_lancamento}' nos últimos "
            f"{meses_para_tras} meses - selecione a categoria manualmente."
        )
    except Exception:
        # Defensivo por natureza: este é o caminho de fallback para tipos
        # desconhecidos - se ele mesmo quebrar, o objetivo do item 3 (nunca
        # travar o processamento por falta de dado) seria frustrado.
        logger.exception("Falha inesperada em buscar_regra_historico_por_tipo para tipo '%s'.", tipo_lancamento)
        return None, f"Falha ao consultar histórico para o tipo '{tipo_lancamento}' - selecione a categoria manualmente."


def _tentar_lancamento_automatico_rescisao(
    access_token: str, usuario: str | None, tipo_lancamento: str,
    tabela: dict, data_vencimento_iso: str | None,
) -> dict | None:
    """AUTONOMIA TOTAL para Termo de Rescisão (2026-09-02, decisão explícita
    do usuário): diferente de todo outro tipo desconhecido, que sempre cai
    em revisão manual, uma tabela de rescisão é lançada DIRETO na Conta
    Azul (nunca vai para revisao/, sem clique humano no painel) quando TUDO
    resolve sozinho:
    - funcionário/contato já cadastrado (match exato);
    - categoria e conta financeira FIXAS (REGRA_RESCISAO_FIXA) existem na
      Conta Azul com esse nome exato;
    - todo item do rateio por centro de custo resolve (inclui código
      numérico puro, ex: "3273" -> "3273 - Allied Graphite - Appian" - já
      tratado por _match_centro_custo/buscar_centro_custo, sem precisar de
      mapeamento extra) e a soma bate com o valor total;
    - data de vencimento resolvida (nome do arquivo ou documento).

    Qualquer uma dessas peças faltando faz devolver None - o chamador
    (processar_tabela) cai no caminho normal de revisão manual, já com a
    categoria/conta fixas aparecendo como sugestão."""
    nome_funcionario = tabela.get("funcionario")
    rateio_doc = tabela.get("rateio") or []
    if not nome_funcionario or not data_vencimento_iso or not rateio_doc:
        return None

    contato, tipo_match_contato = buscar_contato(access_token, nome_funcionario)
    if not contato or tipo_match_contato != "exato":
        logger.info("Rescisão de '%s': contato sem match exato - autonomia total não aplicada.", nome_funcionario)
        return None

    categoria, tipo_match_cat = buscar_categoria(access_token, REGRA_RESCISAO_FIXA["categoria_nome"])
    if not categoria or tipo_match_cat != "exato":
        logger.warning(
            "Categoria fixa de rescisão '%s' não encontrada na Conta Azul - autonomia total não aplicada.",
            REGRA_RESCISAO_FIXA["categoria_nome"],
        )
        return None

    conta, tipo_match_conta = buscar_conta_financeira(access_token, REGRA_RESCISAO_FIXA["conta_financeira_nome"])
    if not conta or tipo_match_conta != "exato":
        logger.warning(
            "Conta financeira fixa de rescisão '%s' não encontrada na Conta Azul - autonomia total não aplicada.",
            REGRA_RESCISAO_FIXA["conta_financeira_nome"],
        )
        return None

    valor_total = round(sum(item["valor"] for item in rateio_doc), 2)
    rateio_doc, valor_total, ajustes_negativos = separar_e_ajustar_valores_negativos(rateio_doc, valor_total)

    rateio_calculado, erros_rateio, _centros_faltando = validar_rateio_do_documento_folha(
        access_token, rateio_doc, valor_total,
    )
    if erros_rateio:
        logger.info(
            "Rescisão de '%s': rateio não resolveu sozinho (%s) - autonomia total não aplicada.",
            nome_funcionario, " | ".join(erros_rateio),
        )
        return None

    duplicata = verificacao_duplicidade.verificar_lancamento_similar_existe(
        access_token, valor_total, data_vencimento_iso,
        categoria_nome=categoria["nome"], contato_nome=contato["nome"],
        categoria_id=categoria["id"], contato_id=contato["id"],
    )
    if duplicata:
        logger.warning(
            "Rescisão de '%s' (R$ %s, vencimento %s) parece duplicata (id=%s) - autonomia total não aplicada, "
            "cai para revisão manual.", nome_funcionario, valor_total, data_vencimento_iso, duplicata.get("id"),
        )
        return None

    descricao = f"Termo de Rescisão - {nome_funcionario}"

    dados_lancamento = {
        "tipo_documento": f"folha_{tipo_lancamento}",
        "valor_total": valor_total,
        "numero_documento": f"RESCISAO-{data_vencimento_iso}-{nome_funcionario.replace(' ', '_')}",
        "descricao": descricao,
        # Campo de observação/nota estritamente vazio para lançamentos de
        # folha (2026-09-02, pedido explícito do usuário) - "" (não None)
        # para não cair no texto padrão de _montar_payload_lancamento.
        "observacoes": "",
    }

    # Competência = vencimento para rescisão (confirmado com o usuário,
    # 2026-09-02) - é uma data exata do documento, não um período a partir
    # do qual calcular algo.
    status, resposta = criar_lancamento(
        access_token, dados_lancamento, categoria["id"], contato["id"], data_vencimento_iso,
        rateio_centro_custo=rateio_calculado,
        data_competencia=data_vencimento_iso,
        forma_pagamento=REGRA_RESCISAO_FIXA["forma_pagamento"],
        conta_financeira_id=conta["id"],
    )

    if status != 200:
        logger.error("Erro %s ao lançar rescisão de '%s' automaticamente: %s", status, nome_funcionario, resposta)
        return None

    logger.info(
        "Rescisão de '%s' lançada 100%% automaticamente (sem revisão humana) - protocolo %s.",
        nome_funcionario, resposta.get("protocolo"),
    )
    return {
        "sucesso": True,
        "motivo": f"Termo de Rescisão de '{nome_funcionario}' lançado automaticamente na Conta Azul (autonomia total).",
        "detalhes": {
            "tipo": tipo_lancamento,
            "valor": valor_total,
            "conta_financeira": conta["nome"],
            "protocolo": resposta.get("protocolo"),
            "descricao": descricao,
            "data_vencimento": data_vencimento_iso,
            "data_competencia": data_vencimento_iso,
            "categoria": {"id": categoria["id"], "nome": categoria["nome"]},
            "contato": {"id": contato["id"], "nome": contato["nome"]},
            "conta_financeira_id": conta["id"],
            "rateio_calculado": rateio_calculado,
            "forma_pagamento": REGRA_RESCISAO_FIXA["forma_pagamento"],
            "resposta_criacao": resposta,
            "ajustes_valor_negativo": ajustes_negativos or None,
            "lancado_automaticamente_sem_revisao": True,
        },
    }


def processar_tabela(
    tabela: dict, dados_folha: dict, access_token: str, usuario: str | None = None,
    caminho_documento: str | None = None,
) -> dict:
    """Processa UMA tabela extraída do documento (um tipo de lançamento com seu
    próprio rateio). Retorna um dicionário de resultado.

    caminho_documento: opcional, usado só como última tentativa de achar a
    data de lançamentos de rescisão/salário quando nem a competência nem a
    data individual do documento estiverem disponíveis (ver
    extrair_data_do_nome_arquivo acima).

    Duas situações terminam sem lançar, mas são bem diferentes:
    - Tabela não circulada (ex: Pró-labore): ignorada DE PROPÓSITO, é esperado
      que isso aconteça, não precisa de atenção humana. Marcada com
      resultado["ignorado_propositalmente"] = True.
    - Tipo de tabela desconhecido/sem regra cadastrada: isso é uma LACUNA
      real - precisa ir para revisão humana.

    Em toda falha que não seja "ignorado_propositalmente", o resultado
    inclui resultado["dados_para_edicao"] - os dados já conhecidos até o
    ponto da falha (rateio, valor, sugestões de contato/categoria/conta
    financeira/datas quando calculáveis), para o painel de revisão poder
    montar um formulário de correção sem precisar reprocessar o documento
    do zero pelo Gemini."""
    resultado = {"sucesso": False, "motivo": "", "detalhes": {}}

    tipo_lancamento = tabela.get("tipo")

    # Normalização: o prompt de extração (extrair_resumo_folha.py) instrui a
    # IA a usar "irrf" como valor de "tipo", mas a chave cadastrada em
    # REGRAS_FOLHA é "irrf_folha" - sem isso, toda tabela de IRRF caía em
    # "tipo não reconhecido" mesmo com a regra já definida.
    if tipo_lancamento == "irrf":
        tipo_lancamento = "irrf_folha"

    # TRAVA DE SEGURANÇA - GRRF (2026-09-02, caso real de produção): a IA
    # às vezes classifica um FGTS de RESCISÃO (GRRF, "FGTS Rescisão") como
    # "fgts" comum - isso aplica a fórmula de mês cheio (ultimo_dia_util_do_mes/
    # dia_20_mes_seguinte) num documento que na verdade tem uma data exata
    # própria, calculando o vencimento errado. Não confia só no prompt para
    # evitar isso de novo: se o "tipo" vier "fgts" mas o texto bruto extraído
    # junto da tabela (contexto_bruto) OU o nome do arquivo mencionar
    # GRRF/RESCISÃO/RESCISÓRIO/QUITAÇÃO, força o tipo para "grrf"
    # independente do que a IA classificou.
    if tipo_lancamento == "fgts":
        texto_para_checar = normalizar(
            f"{tabela.get('contexto_bruto') or ''} {os.path.basename(caminho_documento) if caminho_documento else ''}"
        )
        if any(termo in texto_para_checar for termo in _TERMOS_GRRF):
            logger.warning(
                "Tabela classificada como 'fgts' mas texto/nome do arquivo menciona rescisão (%s) - "
                "forçando tipo para 'grrf' (trava de segurança).", texto_para_checar,
            )
            tipo_lancamento = "grrf"

    if not tabela.get("circulada", True):
        resultado["motivo"] = f"Tabela de '{tipo_lancamento}' não está marcada para processamento (não circulada/destacada no documento) - ignorada propositalmente."
        resultado["ignorado_propositalmente"] = True

        # CORREÇÃO (2026-09-03, falso positivo real de produção): antes,
        # este caminho devolvia SEM "dados_para_edicao" - se o documento
        # inteiro só tivesse tabelas assim (nada relevante para lançar), o
        # relatório dizia "SUCESSO" (bug corrigido em monitor_pasta.py) e,
        # mesmo quando ia parar em revisão por outro motivo, o painel não
        # tinha nada para pré-preencher, obrigando a digitar tudo do zero
        # para forçar o lançamento manual de um documento legítimo que a
        # IA só não detectou a marcação visual. Preenche com o que já foi
        # extraído (rateio, valor, tipo, e sugestão de categoria/conta/
        # forma de pagamento quando o tipo já é uma regra conhecida), igual
        # ao padrão usado nos outros caminhos de falha desta função.
        rateio_doc_ignorado = tabela.get("rateio") or []
        regra_conhecida = REGRAS_FOLHA.get(tipo_lancamento) or {}
        nome_funcionario_ignorado = tabela.get("funcionario")

        data_vencimento_ignorada = data_para_iso(tabela.get("data_pagamento_documento"))
        if not data_vencimento_ignorada:
            data_do_nome_ignorada = extrair_data_do_nome_arquivo(caminho_documento)
            if data_do_nome_ignorada:
                data_vencimento_ignorada = data_do_nome_ignorada.isoformat()

        resultado["dados_para_edicao"] = {
            "tipo_lancamento": tipo_lancamento,
            "arquivo_bancario": tabela.get("arquivo_bancario"),
            "rateio_doc": rateio_doc_ignorado,
            "valor_total": round(sum(item["valor"] for item in rateio_doc_ignorado), 2) if rateio_doc_ignorado else 0.0,
            "competencia": dados_folha.get("competencia"),
            "fornecedor_nome_sugerido": nome_funcionario_ignorado or regra_conhecida.get("fornecedor_nome"),
            "categoria_nome_sugerida": regra_conhecida.get("categoria_nome"),
            "conta_financeira_nome_sugerida": regra_conhecida.get("conta_financeira_nome"),
            "forma_pagamento_sugerida": regra_conhecida.get("forma_pagamento"),
            "data_vencimento_sugerida": data_vencimento_ignorada,
            "data_competencia_sugerida": data_vencimento_ignorada,
            "descricao_sugerida": _descricao_tipo_sem_regra(
                tipo_lancamento, nome_funcionario_ignorado, dados_folha.get("competencia"),
                data=data_vencimento_ignorada,
            ),
        }
        return resultado

    if tipo_lancamento not in REGRAS_FOLHA:
        # Calculado sempre (não só quando falta categoria_herdada) - também
        # é usado mais abaixo para decidir a ordem de busca da data (nome
        # do arquivo vs. documento) e para acionar a autonomia total,
        # ambas exclusivas de rescisão.
        tipo_norm_fallback = normalizar((tipo_lancamento or "").replace("_", " "))
        # Cobre tanto "contiver a palavra rescisao" quanto a classificação
        # exata "termo_de_rescisao" que a IA pode devolver - normalizar()
        # já tira acento/maiúscula e o replace("_", " ") junta as palavras,
        # então os dois casos sempre têm "rescis" como substring aqui.
        eh_rescisao = "rescis" in tipo_norm_fallback

        categoria_herdada = None
        conta_financeira_fallback = None

        if eh_rescisao:
            # Regra de negócio FIXA (2026-09-02, decisão explícita do
            # usuário): rescisão usa SEMPRE a mesma categoria/conta
            # financeira, nunca dependente de histórico - e nem vale a
            # pena consultar o histórico pra esse tipo, porque cada
            # colaborador só é demitido uma vez (nome nunca se repete
            # entre documentos), então buscar_regra_historico_por_tipo
            # nunca encontraria nada de qualquer forma.
            categoria_herdada = {"nome": REGRA_RESCISAO_FIXA["categoria_nome"]}
            conta_financeira_fallback = REGRA_RESCISAO_FIXA["conta_financeira_nome"]
        else:
            # "Cérebro de fallback" (item 2): antes de desistir e mandar
            # tudo em branco pra revisão, tenta herdar a categoria de um
            # lançamento anterior do MESMO tipo - buscar_regra_historico_
            # por_tipo nunca lança exceção por si só, mas o try/except aqui
            # é uma segunda camada de segurança: este bloco cobre o caso
            # DESCONHECIDO por definição, e não pode travar o processamento
            # (item 3) não importa o que aconteça ao tentar enriquecer a
            # sugestão.
            try:
                regra_historico, _motivo_historico = buscar_regra_historico_por_tipo(access_token, tipo_lancamento)
                if regra_historico:
                    categoria_herdada = regra_historico.get("categoria")
            except Exception:
                logger.exception("Falha ao tentar herdar categoria do histórico para tipo '%s' - seguindo sem ela.", tipo_lancamento)

            # Fallback adicional (2026-09-02): quando a busca por histórico
            # não acha nada (empresa não tem lançamento anterior com
            # descrição parecida, ou o funcionário é novo) e o tipo
            # desconhecido claramente se refere a salário (ex:
            # "adiantamento_diferenciado"), usa a categoria e a conta
            # financeira já CONFIRMADAS e usadas pela regra fixa
            # "salarios"/"ferias" em vez de deixar em branco à toa. Só é
            # sugestão (segue editável no painel) - nunca lança
            # automaticamente sem revisão humana (diferente de rescisão).
            if not categoria_herdada and "salari" in tipo_norm_fallback:
                categoria_herdada = {"nome": REGRAS_FOLHA["salarios"]["categoria_nome"]}
                conta_financeira_fallback = REGRAS_FOLHA["ferias"]["conta_financeira_nome"]
                logger.info(
                    "Tipo desconhecido '%s' parece salário e o histórico não achou nada - "
                    "usando categoria/conta financeira fixas como sugestão.", tipo_lancamento,
                )

        # Tipos desconhecidos também podem ser documentos INDIVIDUAIS (um
        # funcionário só, ex: um "Termo de Rescisão" num formato novo que
        # ainda não virou regra fixa) - a IA já pode ter preenchido
        # "funcionario"/"data_pagamento_documento" mesmo sem regra
        # cadastrada; aproveita esses dados em vez de descartar tudo.
        nome_funcionario_desconhecido = tabela.get("funcionario")

        def _data_do_documento_desconhecido() -> str | None:
            data_str = tabela.get("data_pagamento_documento")
            if not data_str:
                return None
            data_iso = data_para_iso(data_str)
            if not data_iso:
                logger.warning(
                    "Data '%s' do documento (tipo desconhecido '%s') em formato inválido "
                    "(usuário poderá preencher manualmente).", data_str, tipo_lancamento,
                )
            return data_iso

        def _data_do_nome_arquivo_desconhecido() -> str | None:
            data_do_nome = extrair_data_do_nome_arquivo(caminho_documento)
            return data_do_nome.isoformat() if data_do_nome else None

        # Rescisão é a ÚNICA exceção à ordem padrão (documento primeiro,
        # nome do arquivo como último recurso): caso real de produção
        # mostrou que o nome do arquivo físico do Termo de Rescisão traz a
        # data de pagamento correta com mais confiabilidade que o texto do
        # documento - por isso, só para esse tipo, o nome do arquivo é
        # tentado PRIMEIRO, e o conteúdo do documento vira o fallback (não
        # o contrário). Outros tipos desconhecidos (ex: vale_transporte,
        # bolsa_estagio fora de regra) continuam na ordem padrão.
        if eh_rescisao:
            data_vencimento_desconhecida = _data_do_nome_arquivo_desconhecido() or _data_do_documento_desconhecido()
        else:
            data_vencimento_desconhecida = _data_do_documento_desconhecido() or _data_do_nome_arquivo_desconhecido()

        if data_vencimento_desconhecida:
            logger.info(
                "Data de vencimento resolvida para tipo desconhecido '%s': %s",
                tipo_lancamento, data_vencimento_desconhecida,
            )

        # AUTONOMIA TOTAL (2026-09-02, exclusiva de rescisão - ver
        # REGRA_RESCISAO_FIXA acima), CONDICIONADA À MATURAÇÃO: se contato,
        # categoria/conta fixas, rateio por centro de custo e data TODOS
        # resolverem sozinhos E o padrão já foi aprovado manualmente pelo
        # menos MATURACAO_MINIMA_PARA_AUTONOMIA vezes no painel, o
        # lançamento já sai direto para a Conta Azul aqui, sem passar pelo
        # caminho de revisão manual abaixo - o documento nunca cai em
        # revisao/ nesse caso. Um padrão ainda "imaturo" (poucas aprovações
        # humanas registradas) SEMPRE cai em revisão manual, mesmo que o
        # robô tenha calculado tudo perfeitamente - dá tempo de auditoria
        # humana antes de confiar 100% na automação. Qualquer peça
        # faltando (ou imaturidade) devolve None/pula, e o fluxo continua
        # normalmente (revisão manual, já com a categoria/conta fixas como
        # sugestão).
        if eh_rescisao:
            if documento_maduro_para_autonomia(tipo_lancamento):
                resultado_automatico = _tentar_lancamento_automatico_rescisao(
                    access_token, usuario, tipo_lancamento, tabela, data_vencimento_desconhecida,
                )
                if resultado_automatico:
                    return resultado_automatico
            else:
                logger.info(
                    "Padrão '%s' tem %s aprovação(ões) humana(s) registrada(s) - abaixo do mínimo de %s para "
                    "autonomia total. Indo para revisão manual mesmo que os dados resolvam sozinhos.",
                    padrao_documento_para_maturacao(tipo_lancamento),
                    historico_centros_funcionarios.contagem_aprovacoes_padrao(padrao_documento_para_maturacao(tipo_lancamento)),
                    MATURACAO_MINIMA_PARA_AUTONOMIA,
                )

        descricao_sugerida_desconhecida = _descricao_tipo_sem_regra(
            tipo_lancamento,
            funcionario=nome_funcionario_desconhecido,
            competencia=dados_folha.get("competencia"),
            data=data_vencimento_desconhecida,
        )

        # Mensagem (2026-09-04, pedido explícito do usuário): a versão
        # anterior dizia "não é reconhecido" para TODO tipo que caía neste
        # bloco - inclusive rescisão, que na verdade TEM regra fixa
        # (REGRA_RESCISAO_FIXA) e só está aqui esperando o Contador de
        # Maturação, não por falta de cadastro. Agora a mensagem reflete o
        # motivo real de cada caso:
        # 1. Rescisão (regra fixa, aguardando maturação): deixa claro que
        #    JÁ é reconhecido e mostra o progresso do contador.
        # 2. Tipo sem regra fixa, mas achou algo parecido no histórico:
        #    "sem regra cadastrada ainda" é preciso aqui - de fato não há
        #    regra, só uma sugestão por semelhança.
        # 3. Tipo totalmente desconhecido (nem regra, nem histórico
        #    parecido): mensagem pede cadastro/correção explicitamente, em
        #    vez do "pode ser um tipo novo, ex: IRPJ" genérico de antes.
        if eh_rescisao:
            contagem_atual_rescisao = historico_centros_funcionarios.contagem_aprovacoes_padrao(
                padrao_documento_para_maturacao(tipo_lancamento)
            )
            resultado["motivo"] = (
                f"Tipo de tabela '{tipo_lancamento}' É RECONHECIDO (rescisão) e os dados abaixo foram "
                f"calculados automaticamente com a regra fixa cadastrada - mas este padrão ainda está "
                f"passando pelo Contador de Maturação ({contagem_atual_rescisao}/"
                f"{MATURACAO_MINIMA_PARA_AUTONOMIA} aprovações humanas). Aprove para registrar mais uma "
                f"confirmação; ao atingir o mínimo, rescisões passam a lançar automático sem revisão."
            )
        elif categoria_herdada:
            resultado["motivo"] = (
                f"Tipo de tabela '{tipo_lancamento}' ainda não tem regra fixa cadastrada, mas encontramos "
                f"uma categoria parecida no histórico de lançamentos - confira e corrija se necessário "
                f"antes de aprovar."
            )
        else:
            nome_arquivo_desconhecido = os.path.basename(caminho_documento) if caminho_documento else None
            resultado["motivo"] = (
                f"Tipo de tabela '{tipo_lancamento}'"
                + (f" (arquivo '{nome_arquivo_desconhecido}')" if nome_arquivo_desconhecido else "")
                + " passou por análise, mas não foram encontrados dados salvos para ele (nem regra fixa "
                "cadastrada, nem histórico parecido) - necessário cadastrar este tipo ou apontar o "
                "cadastro correto abaixo."
            )
        resultado["detalhes"] = {"tipo_nao_reconhecido": tipo_lancamento, "rateio": tabela.get("rateio")}
        resultado["dados_para_edicao"] = {
            "tipo_lancamento": tipo_lancamento,
            "arquivo_bancario": tabela.get("arquivo_bancario"),
            "rateio_doc": tabela.get("rateio") or [],
            "valor_total": round(sum(item["valor"] for item in (tabela.get("rateio") or [])), 2),
            "competencia": dados_folha.get("competencia"),
            # Sem regra cadastrada, o fornecedor/conta financeira/forma de
            # pagamento continuam sem sugestão - mas funcionário, data e
            # categoria (quando disponíveis/herdados) já vêm preenchidos,
            # deixando só o que realmente falta para o humano escolher.
            "fornecedor_nome_sugerido": nome_funcionario_desconhecido,
            "categoria_nome_sugerida": (categoria_herdada or {}).get("nome"),
            "conta_financeira_nome_sugerida": conta_financeira_fallback,
            "forma_pagamento_sugerida": None,
            "data_vencimento_sugerida": data_vencimento_desconhecida,
            "data_competencia_sugerida": data_vencimento_desconhecida,
            "descricao_sugerida": descricao_sugerida_desconhecida,
        }
        return resultado

    regra = REGRAS_FOLHA[tipo_lancamento]

    # CORREÇÃO (item 3, 2026-09-02): todo o processamento de um tipo
    # RECONHECIDO fica isolado nesta chamada, envolvida em try/except - se
    # algo inesperado acontecer no meio do caminho (dado faltando de um
    # jeito que nenhuma checagem específica previu, erro de rede não
    # tratado, etc.), o resultado é uma tabela marcada para revisão humana
    # com o máximo de dados_para_edicao possível, nunca uma exceção não
    # tratada subindo até travar o processamento do documento inteiro.
    try:
        return _processar_tabela_tipo_reconhecido(
            tabela, dados_folha, access_token, usuario, tipo_lancamento, regra, resultado, caminho_documento,
        )
    except Exception:
        logger.exception(
            "Erro inesperado ao processar tabela de tipo reconhecido '%s' - devolvendo dados parciais para revisão em vez de travar.",
            tipo_lancamento,
        )
        rateio_doc_erro = tabela.get("rateio") or []
        resultado["sucesso"] = False
        resultado["motivo"] = (
            f"Erro inesperado ao processar tabela de '{tipo_lancamento}' - REQUER REVISÃO HUMANA. "
            f"Veja os logs para detalhes técnicos."
        )
        resultado["dados_para_edicao"] = {
            "tipo_lancamento": tipo_lancamento,
            "arquivo_bancario": tabela.get("arquivo_bancario"),
            "rateio_doc": rateio_doc_erro,
            "valor_total": round(sum(item["valor"] for item in rateio_doc_erro), 2) if rateio_doc_erro else 0.0,
            "competencia": dados_folha.get("competencia"),
            "fornecedor_nome_sugerido": tabela.get("funcionario") or regra.get("fornecedor_nome"),
            "categoria_nome_sugerida": regra.get("categoria_nome"),
            "conta_financeira_nome_sugerida": regra.get("conta_financeira_nome"),
            "forma_pagamento_sugerida": regra.get("forma_pagamento"),
            "data_vencimento_sugerida": None,
            "data_competencia_sugerida": None,
            "descricao_sugerida": _descricao_tipo_sem_regra(
                tipo_lancamento, tabela.get("funcionario"), dados_folha.get("competencia"),
                data=tabela.get("data_pagamento_documento"),
            ),
        }
        return resultado


def _processar_tabela_tipo_reconhecido(
    tabela: dict, dados_folha: dict, access_token: str, usuario: str | None,
    tipo_lancamento: str, regra: dict, resultado: dict, caminho_documento: str | None = None,
) -> dict:
    """Corpo de processar_tabela para um tipo de lançamento COM regra
    cadastrada em REGRAS_FOLHA - extraído para uma função própria só para
    poder ser envolvido num try/except em processar_tabela sem precisar
    reindentar o corpo inteiro (ver comentário no ponto de chamada)."""
    rateio_doc = tabela.get("rateio") or []

    if not rateio_doc:
        resultado["motivo"] = f"Tabela de '{tipo_lancamento}' não trouxe nenhum centro de custo com valor."
        return resultado

    valor_total = round(sum(item["valor"] for item in rateio_doc), 2)

    logger.info(
        "Processando lançamento: %s (%s) - valor total R$ %s",
        tipo_lancamento.upper(), tabela.get("arquivo_bancario") or "sem banco específico", valor_total,
    )

    # Nome do funcionário (só relevante para ferias/pro_labore - regra com
    # fornecedor_nome=None sinaliza "usar o funcionário do documento em vez
    # de um fornecedor fixo").
    nome_funcionario = tabela.get("funcionario")
    fornecedor_para_buscar = regra["fornecedor_nome"] or nome_funcionario

    # De onde veio o centro de custo usado neste lançamento - "documento"
    # (padrão: já veio marcado no documento, ou o tipo nem usa a cascata de
    # ferias/pro_labore), "historico" (resolvido consultando lançamentos
    # anteriores do funcionário) ou "revisao_manual" (nem documento nem
    # histórico tinham - fica para quem revisar escolher). Atualizado mais
    # abaixo, no bloco que resolve CENTRO_CUSTO_PLACEHOLDER.
    fonte_centro_custo = "documento"

    # Datas e descrição calculadas cedo (antes das checagens que podem
    # falhar), para já ficarem disponíveis no dados_para_edicao em
    # qualquer ponto de falha daqui pra frente.
    #
    # Dois caminhos: tipos com fórmula fixa (FGTS, INSS, etc. - calculam a
    # partir da competência da empresa toda) vs. ferias/pro_labore, que
    # usam a data escrita no PRÓPRIO documento (regra["data_competencia_func"]
    # é None para esses - sinal para não tentar calcular por fórmula).
    data_competencia = data_vencimento = None
    descricao_sugerida = _descricao_tipo_sem_regra(
        tipo_lancamento, competencia=dados_folha.get("competencia"), funcionario=nome_funcionario,
    )

    if regra.get("data_competencia_func") is None:
        # Caminho ferias/pro_labore/grrf: data vem do documento, não de fórmula.
        data_str = tabela.get("data_pagamento_documento")
        if data_str:
            data_unica = parsear_data(data_str)
            if data_unica:
                data_competencia = data_vencimento = data_unica
            else:
                logger.warning(
                    "Data '%s' do documento em formato inválido para %s (usuário poderá preencher manualmente).",
                    data_str, tipo_lancamento,
                )

        if data_competencia is None and tipo_lancamento == "grrf":
            # Fallback (2026-09-02, trava de GRRF): quando o campo "Pagar
            # este documento até" não veio ou não deu pra ler, tenta a data
            # escrita no nome do arquivo - NUNCA cai para uma fórmula de mês
            # cheio (data_competencia_func é None de propósito pra esse
            # tipo, exatamente pra evitar isso).
            data_do_nome = extrair_data_do_nome_arquivo(caminho_documento)
            if data_do_nome:
                data_competencia = data_vencimento = data_do_nome
                logger.info("Data de GRRF extraída do nome do arquivo: %s", data_do_nome.isoformat())

        periodo_gozo = tabela.get("periodo_gozo")
        if nome_funcionario or periodo_gozo:
            # periodo_gozo é usado pelo template de "ferias" (padrão
            # contábil validado); funcionario é usado por "pro_labore"/
            # "grrf". Passar os dois sempre, com periodo_gozo caindo pro
            # nome do funcionário quando o documento não trouxer aquela
            # linha, garante que a descrição nunca fique vazia mesmo se o
            # texto exato do período de gozo não for encontrado.
            descricao_sugerida = regra["descricao_template"].format(
                funcionario=nome_funcionario or "",
                periodo_gozo=periodo_gozo or nome_funcionario or "",
            )
    else:
        try:
            # parsear_competencia (2026-09-14) no lugar de
            # `competencia.split("/")[::-1]`: aceita tanto "MM/AAAA" quanto
            # "AAAA/MM" e devolve None (em vez de explodir ou calcular com
            # mês 2026) quando a competência não veio ou veio ilegível -
            # o `raise ValueError` abaixo mantém esse caso caindo no mesmo
            # tratamento de antes, com dados_para_edicao preenchido.
            competencia_parseada = parsear_competencia(dados_folha.get("competencia"))
            if not competencia_parseada:
                raise ValueError(f"competência ausente ou ilegível: {dados_folha.get('competencia')!r}")
            ano, mes = competencia_parseada
            data_competencia = regra["data_competencia_func"](ano, mes)
            data_vencimento = regra["vencimento_func"](ano, mes)
            # Datas na DESCRIÇÃO sempre em DD/MM/AAAA (bug 2026-09-14: a IA
            # às vezes devolve data_emissao no padrão ISO, e o texto cru ia
            # para a descrição do lançamento invertido, ex: "2026/05/20").
            data_pagamento_para_descricao = data_para_texto_br(
                dados_folha.get("data_emissao") or dados_folha.get("vencimento_documento")
            )
            descricao_sugerida = regra["descricao_template"].format(
                competencia=competencia_para_texto_br(dados_folha.get("competencia")),
                data_pagamento=data_pagamento_para_descricao,
            )
        except (KeyError, ValueError, AttributeError) as e:
            logger.warning("Não foi possível calcular datas para %s: %s (usuário poderá preencher manualmente).", tipo_lancamento, e)

            # Fallback (2026-09-02, caso real de produção): "salarios" às
            # vezes é na verdade um Termo de Rescisão individual sem
            # competência da empresa toda - o nome do arquivo físico traz a
            # data de pagamento exata (ex:
            # "SALARIOS_SEMBANCO_0_2026-08-26.pdf"). Usa essa data
            # DIRETAMENTE como vencimento/competência, sem nenhuma fórmula
            # (ultimo_dia_util_do_mes/dia_20_mes_seguinte não fazem sentido
            # aqui - já é a data exata, não uma competência a partir da
            # qual calcular algo).
            if any(p in normalizar(tipo_lancamento) for p in ("salari", "rescis")):
                data_do_nome = extrair_data_do_nome_arquivo(caminho_documento)
                if data_do_nome:
                    data_competencia = data_vencimento = data_do_nome
                    descricao_sugerida = _descricao_tipo_sem_regra(
                        tipo_lancamento, funcionario=tabela.get("funcionario"), data=data_do_nome,
                    )
                    logger.info(
                        "Data extraída do nome do arquivo para %s: %s", tipo_lancamento, data_do_nome.isoformat(),
                    )

    def _dados_para_edicao_parcial(**overrides) -> dict:
        base = {
            "tipo_lancamento": tipo_lancamento,
            "arquivo_bancario": tabela.get("arquivo_bancario"),
            "rateio_doc": rateio_doc,
            "valor_total": valor_total,
            "competencia": dados_folha.get("competencia"),
            "fornecedor_nome_sugerido": fornecedor_para_buscar,
            "categoria_nome_sugerida": regra["categoria_nome"],
            "conta_financeira_nome_sugerida": regra.get("conta_financeira_nome"),
            "forma_pagamento_sugerida": regra.get("forma_pagamento"),
            "data_vencimento_sugerida": data_vencimento.isoformat() if data_vencimento else None,
            "data_competencia_sugerida": data_competencia.isoformat() if data_competencia else None,
            "descricao_sugerida": descricao_sugerida,
            "fonte_centro_custo": fonte_centro_custo,
        }
        base.update(overrides)
        return base

    if fornecedor_para_buscar is None:
        # Só acontece se for ferias/pro_labore (fornecedor_nome=None na
        # regra) e a IA não conseguiu extrair o nome do funcionário do
        # documento - sem isso, não há quem buscar como contato.
        resultado["motivo"] = (
            f"Não foi possível identificar o funcionário/beneficiário no documento de "
            f"'{tipo_lancamento}' - selecione o contato manualmente."
        )
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial()
        return resultado

    contato, tipo_match_contato = buscar_contato(access_token, fornecedor_para_buscar)
    if not contato or tipo_match_contato != "exato":
        resultado["motivo"] = f"Fornecedor '{fornecedor_para_buscar}' não encontrado na Conta Azul."
        resultado["cadastros_pendentes"] = [{
            "tipo": "contato", "nome_no_documento": fornecedor_para_buscar,
            "sugestao_parecida": contato["nome"] if contato else None,
        }]
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial()
        return resultado

    categoria, tipo_match_cat = buscar_categoria(access_token, regra["categoria_nome"])
    if not categoria or tipo_match_cat != "exato":
        resultado["motivo"] = f"Categoria fixa '{regra['categoria_nome']}' não encontrada na Conta Azul."
        resultado["cadastros_pendentes"] = [{
            "tipo": "categoria", "nome_no_documento": regra["categoria_nome"],
            "sugestao_parecida": categoria["nome"] if categoria else None,
        }]
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial()
        return resultado

    conta_financeira_id, conta_financeira_nome, erro_conta = resolver_conta_financeira(access_token, regra, tabela)
    if erro_conta:
        resultado["motivo"] = erro_conta
        nome_conta_buscada = regra.get("conta_financeira_nome") or ARQUIVO_BANCARIO_PARA_CONTA.get(
            (tabela.get("arquivo_bancario") or "").strip().lower()
        )
        if nome_conta_buscada:
            resultado["cadastros_pendentes"] = [{
                "tipo": "conta_financeira", "nome_no_documento": nome_conta_buscada, "sugestao_parecida": None,
            }]
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial()
        return resultado

    # Ferias/pro_labore: o documento não traz centro de custo (é um recibo
    # individual, não uma tabela de rateio) - o campo vem com o placeholder
    # CENTRO_CUSTO_PLACEHOLDER, e o centro de custo real é resolvido aqui,
    # consultando o histórico de lançamentos anteriores desse funcionário.
    if nome_funcionario and any(item.get("centro_custo") == CENTRO_CUSTO_PLACEHOLDER for item in rateio_doc):
        centro_historico, motivo_falha_historico = buscar_centro_custo_historico_funcionario(access_token, nome_funcionario)
        if centro_historico:
            fonte_centro_custo = "historico"
            for item in rateio_doc:
                if item.get("centro_custo") == CENTRO_CUSTO_PLACEHOLDER:
                    item["centro_custo"] = centro_historico["nome"]
        else:
            fonte_centro_custo = "revisao_manual"
            resultado["motivo"] = motivo_falha_historico
            resultado["cadastros_pendentes"] = [{
                "tipo": "centro_custo", "nome_no_documento": f"(funcionário: {nome_funcionario})",
                "sugestao_parecida": None,
            }]
            resultado["dados_para_edicao"] = _dados_para_edicao_parcial(rateio_doc=rateio_doc, valor_total=valor_total)
            return resultado

    rateio_doc, valor_total, ajustes_negativos = separar_e_ajustar_valores_negativos(rateio_doc, valor_total)
    if ajustes_negativos:
        logger.info("Ajustes por valores negativos no rateio: %s", ajustes_negativos)

    rateio_calculado, erros_rateio, centros_custo_faltando = validar_rateio_do_documento_folha(access_token, rateio_doc, valor_total)
    if erros_rateio:
        resultado["motivo"] = " | ".join(erros_rateio)
        resultado["cadastros_pendentes"] = centros_custo_faltando
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial(rateio_doc=rateio_doc, valor_total=valor_total)
        return resultado

    if data_competencia is None or data_vencimento is None:
        if regra.get("data_competencia_func") is None:
            resultado["motivo"] = (
                f"Não foi possível ler a data de pagamento no documento de '{tipo_lancamento}' "
                f"(campo data_pagamento_documento ausente ou em formato inválido) - preencha manualmente."
            )
        else:
            resultado["motivo"] = f"Campo 'competencia' do documento em formato inválido (esperado MM/AAAA): '{dados_folha.get('competencia')}'"
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial(rateio_doc=rateio_doc, valor_total=valor_total)
        return resultado

    descricao = descricao_sugerida

    numero_doc_sufixo = tabela.get("arquivo_bancario") or "PADRAO"
    # numero_documento usa a competência quando existe (FGTS/INSS/etc);
    # para ferias/pro_labore (sem competência da empresa toda), usa a data
    # de pagamento do documento + o nome do funcionário como identificador
    # único em vez disso.
    if dados_folha.get("competencia"):
        sufixo_identificador = dados_folha["competencia"].replace("/", "-")
    elif data_vencimento:
        sufixo_identificador = f"{data_vencimento.isoformat()}-{(nome_funcionario or '').replace(' ', '_')}"
    else:
        sufixo_identificador = (nome_funcionario or "SEMID").replace(" ", "_")

    dados_lancamento = {
        "tipo_documento": f"folha_{tipo_lancamento}",
        "valor_total": valor_total,
        "numero_documento": f"{tipo_lancamento.upper()}-{numero_doc_sufixo}-{sufixo_identificador}",
        "descricao": descricao,
        # Campo de observação/nota estritamente vazio para lançamentos de
        # folha (2026-09-02, pedido explícito do usuário) - "" (não None)
        # para não cair no texto padrão de _montar_payload_lancamento.
        "observacoes": "",
    }

    # Camada 2 de proteção contra duplicata (ver verificacao_duplicidade.py):
    # checagem AO TELECOM EXEMPLO, POR TABELA - é o que protege o caso real observado em
    # uso (folha com sucesso parcial reprocessada: as tabelas que já foram
    # lançadas não devem ser lançadas de novo, mesmo que outras tabelas do
    # mesmo documento ainda precisem ser corrigidas e relançadas).
    duplicata = verificacao_duplicidade.verificar_lancamento_similar_existe(
        access_token, valor_total, data_vencimento.isoformat(),
        categoria_nome=categoria["nome"], contato_nome=contato["nome"],
        categoria_id=categoria["id"], contato_id=contato["id"],
    )
    if duplicata:
        resultado["motivo"] = (
            f"POSSÍVEL DUPLICATA: já existe um lançamento na Conta Azul com valor R$ {valor_total} "
            f"e vencimento {data_vencimento.isoformat()} (id={duplicata.get('id')}, "
            f"status={duplicata.get('status')}) - provavelmente esta tabela ({tipo_lancamento.upper()} "
            f"{numero_doc_sufixo}) já foi lançada antes. Não lançado de novo automaticamente."
        )
        resultado["possivel_duplicata"] = duplicata
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial(rateio_doc=rateio_doc, valor_total=valor_total)
        logger.warning("Tabela %s bloqueada por possível duplicata: %s", tipo_lancamento, resultado["motivo"])
        return resultado

    # AUTONOMIA CONDICIONAL (2026-09-03, pedido explícito do usuário -
    # "Inconsistência de Proteção" apontada em auditoria): documentos
    # INDIVIDUAIS reconhecidos (grrf/ferias/pro_labore - identificados pela
    # própria regra ter fornecedor_nome=None, sinal de que usam
    # tabela["funcionario"] em vez de um fornecedor fixo da empresa) têm o
    # MESMO perfil de risco que rescisão: cada documento é de um
    # funcionário diferente, o nome nunca se repete. Por isso ganham a
    # MESMA proteção - só lançam automaticamente depois que o padrão já
    # foi aprovado manualmente pelo menos MATURACAO_MINIMA_PARA_AUTONOMIA
    # vezes no painel (ver documento_maduro_para_autonomia). Tipos com
    # fórmula fixa da empresa toda (fgts/inss/salarios/bolsa_estagio/
    # adiantamento_salarial - fornecedor_nome preenchido) NÃO são afetados
    # - continuam lançando automaticamente como sempre, porque a regra já
    # é fixa e validada há mais tempo, não uma classificação ad-hoc por
    # documento.
    if regra.get("fornecedor_nome") is None and not documento_maduro_para_autonomia(tipo_lancamento):
        padrao = padrao_documento_para_maturacao(tipo_lancamento)
        contagem_atual = historico_centros_funcionarios.contagem_aprovacoes_padrao(padrao)
        resultado["motivo"] = (
            f"Padrão '{padrao}' tem {contagem_atual} aprovação(ões) humana(s) registrada(s) - abaixo do "
            f"mínimo de {MATURACAO_MINIMA_PARA_AUTONOMIA} para autonomia total. Todos os dados foram "
            f"calculados corretamente, mas ainda requer confirmação humana até o padrão amadurecer."
        )
        resultado["dados_para_edicao"] = _dados_para_edicao_parcial(rateio_doc=rateio_doc, valor_total=valor_total)
        logger.info(
            "Tabela %s (%s) resolveu todos os dados sozinha, mas o padrão '%s' ainda não está maduro "
            "(%s/%s aprovações) - indo para revisão manual mesmo assim.",
            tipo_lancamento, numero_doc_sufixo, padrao, contagem_atual, MATURACAO_MINIMA_PARA_AUTONOMIA,
        )
        return resultado

    status, resposta = criar_lancamento(
        access_token, dados_lancamento, categoria["id"], contato["id"],
        data_vencimento.isoformat(),
        rateio_centro_custo=rateio_calculado,
        data_competencia=data_competencia.isoformat(),
        forma_pagamento=regra["forma_pagamento"],
        conta_financeira_id=conta_financeira_id,
    )

    if status == 200:
        resultado["sucesso"] = True
        resultado["motivo"] = f"Lançamento de {tipo_lancamento.upper()} ({numero_doc_sufixo}) criado com sucesso"
        resultado["detalhes"] = {
            "tipo": tipo_lancamento,
            "arquivo_bancario": tabela.get("arquivo_bancario"),
            "valor": valor_total,
            "conta_financeira": conta_financeira_nome,
            "protocolo": resposta.get("protocolo"),
            "ajustes_valor_negativo": ajustes_negativos or None,
            "fonte_centro_custo": fonte_centro_custo,
            # Guardado para o Histórico local poder reconstruir um
            # formulário de edição sem depender de match aproximado por
            # nome depois - IDs, não só texto (mesmo padrão de
            # agente_integrado.processar_documento).
            "descricao": descricao,
            "data_vencimento": data_vencimento.isoformat(),
            "data_competencia": data_competencia.isoformat(),
            "categoria": {"id": categoria["id"], "nome": categoria["nome"]},
            "contato": {"id": contato["id"], "nome": contato["nome"]},
            "conta_financeira_id": conta_financeira_id,
            "rateio_calculado": rateio_calculado,
            "forma_pagamento": regra["forma_pagamento"],
            "resposta_criacao": resposta,
        }

        if ajustes_negativos:
            resultado["atencao_necessaria"] = (
                "Este lançamento foi criado com um valor COMPLEMENTADO por causa de "
                "centro(s) de custo com crédito/estorno (valor negativo). O pagamento "
                "à Conta Azul já reflete o valor correto, mas é necessário que o "
                "financeiro lance manualmente o valor correspondente como CONTAS A "
                "RECEBER, referente ao(s) crédito(s) abaixo:"
            )
            logger.warning("ATENÇÃO NECESSÁRIA - REVISÃO HUMANA: %s | ajustes=%s", resultado["atencao_necessaria"], ajustes_negativos)
    else:
        resultado["motivo"] = "Erro ao lançar. Veja os logs para detalhes."
        logger.error("Erro %s ao lançar tabela %s: %s", status, tipo_lancamento, resposta)

    return resultado


def processar_documento_folha(dados_folha: dict, usuario: str | None = None, caminho_documento: str | None = None) -> dict:
    """Processa TODAS as tabelas encontradas no documento (já extraídas),
    pulando as não marcadas para processamento (circulada=false) e as de tipo
    não reconhecido. Retorna um dicionário de resultados, uma entrada por
    tabela processada.

    CORRIGIDO: aceita `usuario` (ver nota no topo do arquivo sobre o bug de
    duplicação que fazia essa função "esquecer" o parâmetro).

    caminho_documento: opcional, repassado para processar_tabela (ver
    extrair_data_do_nome_arquivo) - permite achar a data de rescisão/
    salário quando o documento não traz competência nem data individual."""
    access_token = obter_access_token()
    resultados = {}

    tabelas = dados_folha.get("tabelas") or []
    if not tabelas:
        return {"indefinido": {
            "sucesso": False,
            "motivo": "Nenhuma tabela foi identificada no documento.",
            "detalhes": dados_folha,
        }}

    for i, tabela in enumerate(tabelas):
        chave = f"{tabela.get('tipo') or 'desconhecido'}_{tabela.get('arquivo_bancario') or 'sembanco'}_{i}"
        try:
            resultados[chave] = processar_tabela(tabela, dados_folha, access_token, usuario, caminho_documento)
        except Exception:
            # Uma tabela com erro inesperado não deve impedir o processamento
            # das demais tabelas do mesmo documento.
            logger.exception("Erro inesperado ao processar tabela '%s'", chave)
            resultados[chave] = {
                "sucesso": False,
                "motivo": "Erro inesperado ao processar esta tabela. Veja os logs para detalhes.",
                "detalhes": {"tabela": tabela},
            }

    logger.info("Resumo do processamento da folha:")
    for chave, resultado in resultados.items():
        status_txt = "SUCESSO" if resultado["sucesso"] else "IGNORADA/REVISÃO"
        logger.info("%s: %s - %s", chave.upper(), status_txt, resultado["motivo"])

    return resultados


def processar_resumo_folha(caminho_documento: str, usuario: str | None = None) -> dict:
    """Processa um documento de folha do zero: extrai os dados via IA e gera
    um lançamento para cada tabela marcada e reconhecida."""
    logger.info("Processando documento de folha: %s", caminho_documento)
    dados_folha = extrair_dados_folha(caminho_documento)
    return processar_documento_folha(dados_folha, usuario, caminho_documento)


if __name__ == "__main__":
    config.validar_configuracao()
    if len(sys.argv) > 1:
        resultado = processar_resumo_folha(sys.argv[1])
        print(json.dumps(resultado, indent=2, ensure_ascii=False))
    else:
        print("Uso: python agente_folha.py <caminho_do_pdf>")