"""
Helpers de formatação usados na montagem de textos que o usuário vê no ERP
(principalmente o campo "Descrição" do lançamento na Conta Azul).

Existe por causa de dois bugs reais de produção (2026-09-14), os dois
visíveis direto na tela de detalhe do lançamento:

1. Descrição saindo com o literal "None" (ex: "beneficios - None").
   Causa: `dados.get("chave", "padrao")` NÃO devolve "padrao" quando a
   chave EXISTE com valor None - devolve None mesmo, e a f-string então
   imprime "None". E os schemas do projeto (schemas.py) declaram vários
   desses campos como `Optional[str] = None`, ou seja: a chave quase
   sempre existe, o valor é que vem nulo. Use `texto_ou()` no lugar.

2. Data aparecendo invertida na descrição (ex: "2026/05/20" em vez de
   "20/05/2026"). Causa: parte das descrições era montada com
   `data.isoformat()` (formato da API, AAAA-MM-DD) e parte repassava, sem
   conferir, a string crua devolvida pela IA - que às vezes ignora o
   "DD/MM/AAAA" pedido no prompt e responde no padrão americano/ISO.
   Use `data_para_texto_br()` em tudo que for texto lido por humano.

IMPORTANTE: o formato DD/MM/AAAA é só para TEXTO (descrição, observação,
mensagem de log). Os campos de data enviados para a API da Conta Azul
(data_vencimento, data_competencia) e os campos "..._sugerida" do painel
continuam em ISO (AAAA-MM-DD) - para esses, use `data_para_iso()`.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# Aceita separador "/", "-" ou "." em qualquer das duas ordens.
_REGEX_DATA = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})\s*$")
# Competência é mês/ano (sem dia) - mesma bagunça de ordem: "05/2026" (o
# que o prompt pede) ou "2026/05" (o que a IA às vezes devolve).
_REGEX_COMPETENCIA = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,4})\s*$")


def texto_ou(valor, padrao: str = "") -> str:
    """Devolve `valor` como texto limpo, ou `padrao` quando ele for None,
    vazio ou só espaços.

    Serve para nunca deixar um None virar a string "None" dentro de uma
    f-string - ver o bug 1 no topo do módulo."""
    if valor is None:
        return padrao
    texto = str(valor).strip()
    return texto if texto else padrao


def parsear_data(valor) -> date | None:
    """Converte uma data em `date`, aceitando os formatos que realmente
    aparecem no projeto: objetos date/datetime, "DD/MM/AAAA" (o que o
    prompt de extração pede) e "AAAA-MM-DD"/"AAAA/MM/DD" (o que a IA às
    vezes devolve, e o formato da API da Conta Azul).

    Devolve None quando não dá para interpretar com segurança - quem chama
    decide o fallback (normalmente: deixar o campo em branco para
    preenchimento manual no painel). Nunca lança exceção."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor

    m = _REGEX_DATA.match(str(valor))
    if not m:
        return None

    primeiro, meio, ultimo = m.group(1), m.group(2), m.group(3)

    # Qual dos extremos é o ano: quem tiver 4 dígitos. Com 4 dígitos dos
    # dois lados (impossível na prática) ou nenhum, não há como decidir
    # sem chutar - e chutar aqui viria a virar lançamento com data errada.
    if len(primeiro) == 4 and len(ultimo) != 4:
        ano, mes, dia = primeiro, meio, ultimo
    elif len(ultimo) == 4 and len(primeiro) != 4:
        dia, mes, ano = primeiro, meio, ultimo
    else:
        return None

    try:
        return date(int(ano), int(mes), int(dia))
    except ValueError:
        # Data sintaticamente parecida mas inexistente (ex: 31/02/2026).
        return None


def data_para_texto_br(valor, padrao: str = "") -> str:
    """Formata uma data como DD/MM/AAAA, para TEXTO lido por humano
    (descrição do lançamento, observação, mensagem).

    Aceita os mesmos formatos de `parsear_data`. Quando não consegue
    interpretar, devolve o texto original (sem o "None" do bug 1) em vez
    de apagar a informação - uma data em formato estranho na descrição
    ainda é mais útil para quem revisa do que um campo vazio."""
    data = parsear_data(valor)
    if data is not None:
        return data.strftime("%d/%m/%Y")
    return texto_ou(valor, padrao)


def parsear_competencia(valor) -> tuple[int, int] | None:
    """Converte uma competência em `(ano, mes)`, aceitando "MM/AAAA" (o
    formato pedido no prompt de extração) e "AAAA/MM" (o que a IA às vezes
    devolve). Devolve None quando não dá para interpretar com segurança.

    Existe para o cálculo de datas por fórmula (ultimo_dia_util_do_mes e
    companhia) não depender da ordem em que a IA escreveu a competência -
    antes, `competencia.split("/")[::-1]` assumia MM/AAAA cegamente e uma
    resposta em AAAA/MM virava "mês 2026", jogando o documento inteiro em
    revisão manual sem motivo real."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.year, valor.month
    if isinstance(valor, date):
        return valor.year, valor.month

    m = _REGEX_COMPETENCIA.match(str(valor))
    if not m:
        return None

    primeiro, ultimo = m.group(1), m.group(2)
    if len(primeiro) == 4 and len(ultimo) != 4:
        ano, mes = primeiro, ultimo
    elif len(ultimo) == 4 and len(primeiro) != 4:
        mes, ano = primeiro, ultimo
    else:
        return None

    ano_int, mes_int = int(ano), int(mes)
    if not 1 <= mes_int <= 12:
        return None
    return ano_int, mes_int


def competencia_para_texto_br(valor, padrao: str = "") -> str:
    """Formata uma competência como MM/AAAA, para TEXTO lido por humano.
    Igual a `data_para_texto_br`: o que não for reconhecido volta como o
    texto original, nunca como "None"."""
    comp = parsear_competencia(valor)
    if comp is not None:
        ano, mes = comp
        return f"{mes:02d}/{ano:04d}"
    return texto_ou(valor, padrao)


def data_para_iso(valor, padrao: str | None = None) -> str | None:
    """Formata uma data como AAAA-MM-DD, formato exigido pela API da Conta
    Azul e pelos campos de data do painel de revisão.

    Ao contrário de `data_para_texto_br`, NÃO devolve o texto original
    quando não consegue interpretar: manda o `padrao` (None por padrão).
    Um valor não reconhecido aqui iria direto para a API - melhor cair no
    caminho de "preencha manualmente" do que enviar lixo."""
    data = parsear_data(valor)
    return data.isoformat() if data is not None else padrao
