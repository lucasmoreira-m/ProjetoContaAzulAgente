"""
Extração de dados de documentos de folha de pagamento via Gemini.

Prompt e regra de negócio mantidos EXATAMENTE como no original - a única
mudança é técnica: uso de config.py para a chave de API e o modelo, e
validação do JSON retornado contra schemas.DocumentoFolhaExtraido antes de
devolver os dados para o resto do sistema.
"""

from __future__ import annotations

import json
import re
import sys
import time

from google import genai
from google.genai import errors as genai_errors

from src.core import config
from src.utils import gemini_inline_upload
from src.utils import historico_centros_funcionarios
from src.utils.regras_folha_pagamento import CENTRO_CUSTO_PLACEHOLDER
from src.utils.schemas import validar_documento_folha

logger = config.get_logger(__name__)

_client_gemini: genai.Client | None = None


def _cliente_gemini() -> genai.Client:
    global _client_gemini
    if _client_gemini is None:
        _client_gemini = genai.Client(api_key=config.gemini_api_key())
    return _client_gemini


def remover_linhas_totais(rateio_lista: list[dict]) -> list[dict]:
    """Filtro de segurança: remove qualquer linha que pareça ser um total/soma
    da própria tabela (ex: 'Empresa Total', 'Total Geral'), em vez de um
    centro de custo de verdade. Um centro de custo real sempre tem um código
    numérico no início (ex: '3233 - Cedro - Hospitais'); linhas de total não têm.

    CORREÇÃO (2026-09-01), duas partes:
    1. O placeholder CENTRO_CUSTO_PLACEHOLDER ("A_DEFINIR", usado em documentos
       individuais de ferias/pro_labore quando o documento não marca centro de
       custo nenhum) também não tem código numérico - estava sendo removido
       por este filtro como se fosse uma linha de total, o que apagava o valor
       da tabela inteira e impedia agente_folha.py de sequer tentar resolver o
       centro de custo pelo histórico do funcionário (bug real observado em
       produção: documento de férias caía em revisão com "não trouxe nenhum
       centro de custo com valor", mesmo tendo o valor certinho).
    2. Anotações visuais em documentos individuais às vezes trazem só o
       código, sem hífen nem nome (ex: "C. RESP: 1122" -> centro_custo="1122",
       sem "- Administrativo" atrás, diferente das tabelas consolidadas de
       FGTS/INSS onde o nome completo do centro de custo já vem escrito). O
       regex exigia um "-" depois do número e descartava esse caso também -
       agora basta o código começar com dígito; a checagem de "total" abaixo
       continua excluindo linhas de resumo mesmo que comecem com número."""
    filtrado = []
    for item in rateio_lista:
        nome = item.get("centro_custo", "")
        tem_codigo_numerico = bool(re.match(r"^\s*\d+", nome))
        e_placeholder = nome == CENTRO_CUSTO_PLACEHOLDER
        parece_total = "total" in nome.lower()
        if (tem_codigo_numerico or e_placeholder) and not parece_total:
            filtrado.append(item)
        else:
            logger.debug("Ignorando linha que parece ser total/resumo, não um centro de custo real: '%s'", nome)
    return filtrado


PROMPT_EXTRACAO_FOLHA = """
FILTRO OBRIGATÓRIO ANTES DE QUALQUER OUTRA COISA (2026-09-14, caso real de
produção): este documento tem ALGUMA relação com folha de pagamento -
resumo de FGTS/INSS/IRRF, pagamento de salário/bolsa de estágio/
pró-labore, férias ou rescisão de UM funcionário/da empresa? Se a resposta
for claramente NÃO (ex: boleto ou fatura de um FORNECEDOR - internet,
telefone, aluguel, software, qualquer serviço comprado -, nota fiscal
comum, comprovante de despesa), retorne imediatamente "tabelas": [] e
IGNORE todas as instruções abaixo sobre localizar centro de custo/rateio,
mesmo que o documento contenha campos com nomes parecidos ("Centro Custo",
"C. RESP", "CC") - MUITOS boletos bancários trazem esse tipo de anotação
como controle INTERNO do próprio banco/pagador, sem nenhuma relação com
folha de pagamento, e extrair uma tabela desse documento (mesmo com "tipo":
null) faz o sistema tratá-lo como folha por engano. Só continue para as
instruções abaixo se a resposta for SIM.

ATENÇÃO VISUAL: Analise toda a página visualmente, incluindo cantos,
bordas, caixas flutuantes, carimbos e anotações em qualquer cor.
Procure especificamente por campos como "C. RESP", "CENTRO", "CC",
"CUSTO", "FINANCEIRO", "ENTRADA". Se encontrar um código numérico
associado a esses campos (ex: "C. RESP: 1122"), use-o como
centro_de_custo. Retorne null se não existir — nunca invente.

IMPORTANTE: "retorne null" acima se refere ao CÓDIGO do centro de custo, não
à linha de "rateio" inteira. Toda tabela de folha de pagamento com um valor
total (mesmo sem código de centro de custo nenhum visível) DEVE ter pelo
menos uma linha em "rateio" - use o valor especial descrito na seção
"DOCUMENTOS INDIVIDUAIS" mais abaixo quando não encontrar código nenhum.
NUNCA devolva "rateio": [] para uma tabela de folha que tem um valor a
lançar - isso descarta o valor inteiro do processamento. Esta regra vale
só para tabelas que já passaram pelo FILTRO OBRIGATÓRIO acima - nunca force
uma linha de rateio num documento que não é de folha de pagamento.

Você é um assistente que extrai dados de documentos de folha de pagamento
(resumos de FGTS/INSS/IRRF, ou arquivos de pagamento de salário/bolsa de
estágio/pró-labore) para lançamentos financeiros.

O documento pode conter VÁRIAS TABELAS distintas, cada uma com seu próprio
total e sua própria lista de centro de custo/valor (também chamado de
centro de resultado). Analise com atenção visual TODAS as páginas.

CENTRO DE RESULTADO / RATEIO: procure o centro de resultado de cada tabela usando QUALQUER sinal
disponível no documento - não dependa de um único tipo de marcação, porque nem todo documento traz
a mesma forma de indicar isso:
- Às vezes há uma LINHA DE CONTORNO VERMELHA ao redor da tabela ou de cada linha (caixa completa,
  sublinhado ou círculo vermelho) - quando existir, é um sinal forte.
- Às vezes é outra cor, destaque, negrito, ou nenhuma marcação visual especial - só texto corrido.
- Às vezes está em anotações, textos sobrepostos ou imagens coladas dentro do PDF.
Examine cada tabela por todos esses sinais possíveis antes de concluir que não há centro de
resultado identificável. As mesmas marcações (de qualquer tipo, vermelhas ou não) também indicam
quais tabelas devem ser processadas (ver campo "circulada" abaixo). NUNCA trate uma anotação
bancária/interna de controle (ex: um campo "Centro Custo/Resultado" impresso pelo PRÓPRIO BANCO
num boleto anexo ao documento, sem relação com a tabela de folha em si) como se fosse uma linha
de "rateio" de alguma das tabelas de folha - só use valores que pertençam de fato à tabela de
folha sendo extraída.

Para CADA tabela que encontrar, identifique:
- "tipo": o tipo de lançamento a que ela se refere. Use pistas como o
  cabeçalho da coluna de valor (ex: "FGTS TOTAL" = fgts; "INSS TOTAL" = inss;
  "SALARIO LÍQUIDO" = salarios; menções a "Bolsa Estágio" = bolsa_estagio;
  menções a "Pró-labore"/"Pro labore" = pro_labore; menções a "Adiantamento" =
  adiantamento_salarial; menções a "Férias"/"Aviso e Recibo de Férias" =
  ferias; menções a "GRRF", "Guia de Recolhimento Rescisório do FGTS" ou
  "Termo de Rescisão" (quando o documento é sobre o FGTS da rescisão de UM
  funcionário) = grrf). Valores possíveis: "fgts", "inss", "irrf",
  "salarios", "bolsa_estagio", "pro_labore", "adiantamento_salarial",
  "ferias", "grrf". Se o documento claramente for um lançamento derivado de
  folha de pagamento mas NÃO se encaixar em nenhum desses tipos fixos (ex:
  um "Aviso Prévio", "13º Salário" ou outro tipo de rescisão diferente de
  GRRF), NÃO use null - escreva seu melhor palpite curto, em snake_case e
  em português, para o tipo do documento (ex: "aviso_previo",
  "decimo_terceiro"), usando palavras que apareçam no próprio título/
  cabeçalho do documento sempre que possível. Isso permite ao sistema
  procurar lançamentos parecidos no histórico, mesmo sem uma regra fixa
  cadastrada ainda. Use null só quando o documento genuinamente não parecer
  ter relação alguma com folha de pagamento.
- "arquivo_bancario": o banco/arquivo a que essa tabela pertence geralmente
  aparece no CABEÇALHO/TÍTULO logo ACIMA da tabela (ex: "Arquivo Bancário -
  Banco Alfa", "Arquivo Bancário - Beta", "Bolsa Estágio - Banco Alfa", "Pagamentos
  Avulsos - Beta"). Procure esse título antes de olhar em outros lugares
  do documento. Extraia só o nome do banco (ex: "Banco Alfa", "Beta", "Banco do
  Brasil"). Se o cabeçalho da tabela não mencionar banco nenhum, use null.
- "rateio": cada linha de centro de custo/departamento/projeto com seu valor,
  exatamente como aparece. NÃO inclua a linha de TOTAL GERAL da própria
  tabela (ex: "Total", "Empresa Total") como se fosse um centro de custo.
- "contexto_bruto": copie o título/cabeçalho dessa tabela e qualquer texto
  próximo (carimbos, anotações, siglas) EXATAMENTE como aparece no
  documento, sem resumir nem traduzir - inclua sempre, mesmo quando "tipo"
  já ficou claro. Serve de checagem independente: por exemplo, se o
  documento menciona "GRRF", "Guia de Recolhimento Rescisório", "Termo de
  Rescisão" ou "Quitação" perto dessa tabela, esse texto precisa aparecer
  aqui mesmo que "tipo" tenha sido classificado como "fgts".

ATENÇÃO - DOCUMENTOS INDIVIDUAIS (tipo "ferias", "pro_labore" ou "grrf"
quando o documento é um recibo/guia de UM único funcionário, não uma
tabela consolidada da empresa toda com vários centros de custo): nesse
caso, preencha também:
- "funcionario": o nome completo do funcionário/beneficiário, exatamente
  como aparece no documento (ex: "MARIA EXEMPLO SOUZA").
- "data_pagamento_documento": a data de pagamento/vencimento específica
  escrita no documento (DD/MM/AAAA) - geralmente perto de "Data:" no fim
  do recibo, ou a data de início do período de gozo das férias. Em
  documentos de GRRF especificamente, essa data está no campo "Pagar este
  documento até" - use esse campo, não outra data do documento (ex: data
  de emissão ou data da rescisão), quando o tipo for "grrf".
- "periodo_gozo" (SOMENTE para tipo "ferias"): varra o texto do recibo e
  capture a linha exata do período de gozo das férias, como aparece no
  documento (ex: "21/09/2026 A 05/10/2026 = 15 Dias" ou "período de gozo
  30/09/2026 A 14/10/2026 = 15 Dias"). Copie o texto tal como está escrito,
  sem reformatar. Deixe null se essa linha não existir no documento.
 - "rateio":
  Se o documento mostrar explicitamente um centro de custo,
  centro de responsabilidade, C. RESP, departamento,
  projeto ou centro de resultado (por exemplo:
  "C. RESP: 1122"), extraia esse valor exatamente como aparece.

  Exemplo:
  {"centro_custo": "1122", "valor": valor_total}

  Somente use:
  {"centro_custo": "A_DEFINIR", "valor": valor_total}

  quando realmente não existir nenhum centro de custo,
  centro de responsabilidade ou identificação equivalente
  visível no documento. O valor "A_DEFINIR" é
  proposital - o centro de custo real desse funcionário é resolvido depois,
  consultando o histórico de lançamentos dele na Conta Azul.

IMPORTANTE: inclua TODAS as tabelas encontradas na lista, mesmo as não
marcadas para processamento (ex: Pró-labore) - use o campo "circulada" para
indicar true/false se está marcada (destaque, círculo, cor) para processar
ou não. Isso permite que quem revisar o resultado veja tudo que existe no
documento, mesmo o que não deve ser lançado.

Extraia também, como referência: nome da empresa, CNPJ, e a competência
(MM/AAAA) - SE o documento tiver esse conceito de "competência da empresa
toda" (comum em resumos consolidados de FGTS/INSS/Salários). Documentos
individuais por funcionário (ex: recibo de férias) geralmente NÃO têm essa
competência - nesse caso, deixe null, sem inventar um valor. Extraia também
data de emissão, e a data de vencimento do documento (DD/MM/AAAA), se
houver uma única data de vencimento mencionada no topo do documento.

RIGOR OBRIGATÓRIO em "competencia" e em "funcionario": procure esses dados
em TODO o documento (cabeçalho, rodapé, título de tabela, texto corrido) -
"competência" pode aparecer como "Competência:", "Mês/Ano de referência",
ou só uma data MM/AAAA perto do título; "funcionario" pode aparecer como
"Funcionário:", "Nome:", "Beneficiário:", ou já destacado no topo do
recibo. EM DOCUMENTOS DE IRRF ESPECIFICAMENTE: se a palavra "competência"
não aparecer em lugar nenhum do documento, procure OBRIGATORIAMENTE por
"Período", "Período de Apuração" ou a sigla "PA" (comum em guias de IRRF) -
a data ali é a competência desse lançamento, mesmo sem usar a palavra
"competência" literalmente. Só retorne null quando o dado genuinamente NÃO
existir em nenhuma parte do documento - nunca por incerteza ou por não ter
procurado com atenção. Isso não contradiz a regra acima (documento individual sem
conceito de competência da empresa toda continua null) - o rigor é sobre
não deixar de EXTRAIR um dado que está visível só porque não apareceu no
lugar mais óbvio.

Retorne APENAS um JSON válido, sem texto antes/depois, sem markdown:
{
  "empresa": "...",
  "cnpj": "...",
  "competencia": "MM/AAAA" ou null,
  "data_emissao": "DD/MM/AAAA",
  "vencimento_documento": "DD/MM/AAAA" ou null,
  "tabelas": [
    {
      "tipo": "salarios" ou "bolsa_estagio" ou "pro_labore" ou "adiantamento_salarial" ou "fgts" ou "inss" ou "irrf" ou "ferias" ou "grrf" ou seu palpite em snake_case para outro tipo de folha, ou null,
      "arquivo_bancario": "Alfa" ou "Beta" ou "Banco do Brasil" ou null,
      "circulada": true ou false,
      "funcionario": "nome do funcionário (só para documentos individuais de ferias/pro_labore)" ou null,
      "data_pagamento_documento": "DD/MM/AAAA (só para documentos individuais)" ou null,
      "periodo_gozo": "texto exato do período de gozo (só para tipo ferias)" ou null,
      "contexto_bruto": "título/cabeçalho e texto próximo dessa tabela, exatamente como aparece" ou null,
      "rateio": [
        {"centro_custo": "nome exatamente como aparece", "valor": numero}
      ]
    }
  ]
}
Se não encontrar nenhuma tabela, retorne "tabelas" como lista vazia.

ORIENTAÇÕES LIVRES DE UM HUMANO (digitadas na tela de revisão de documentos de folha anteriores -
aplique apenas as que forem relevantes para ESTE documento específico, ignore as que não se
aplicarem):
{MARCADOR_DIRETRIZES_LIVRES}
"""

_MARCADOR_DIRETRIZES_LIVRES = "{MARCADOR_DIRETRIZES_LIVRES}"


def extrair_dados_folha(caminho_documento: str, max_tentativas: int = 5) -> dict:
    """Extrai dados de um documento de folha de pagamento e valida o schema
    antes de devolver. Tenta novamente automaticamente se o Gemini estiver
    indisponível temporariamente."""
    diretrizes_livres = historico_centros_funcionarios.buscar_diretrizes_livres("folha")
    prompt_com_diretrizes = PROMPT_EXTRACAO_FOLHA.replace(
        _MARCADOR_DIRETRIZES_LIVRES, historico_centros_funcionarios.formatar_diretrizes_livres_para_prompt(diretrizes_livres),
    )

    for tentativa in range(1, max_tentativas + 1):
        try:
            arquivo = gemini_inline_upload.preparar_parte_inline(caminho_documento)
            resposta = _cliente_gemini().models.generate_content(
                model=config.GEMINI_MODEL,
                contents=[prompt_com_diretrizes, arquivo],
            )
            texto_limpo = resposta.text.strip().replace("```json", "").replace("```", "").strip()

            try:
                dados_brutos = json.loads(texto_limpo)
            except json.JSONDecodeError as e:
                logger.error("Resposta da IA não é um JSON válido: %s", texto_limpo[:500])
                raise RuntimeError("A IA retornou um formato inesperado (não-JSON) para o documento de folha.") from e

            dados_validados = validar_documento_folha(dados_brutos)
            dados = dados_validados.model_dump()

            for tabela in dados.get("tabelas", []):
                tabela["rateio"] = remover_linhas_totais(tabela.get("rateio") or [])

            return dados

        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            espera = 10 * tentativa
            logger.warning(
                "Erro temporário na comunicação com o Gemini (tentativa %s/%s): %s",
                tentativa, max_tentativas, e,
            )
            if tentativa == max_tentativas:
                raise
            time.sleep(espera)

    raise RuntimeError("Não foi possível extrair dados do documento de folha após todas as tentativas.")


if __name__ == "__main__":
    config.validar_configuracao()
    if len(sys.argv) > 1:
        dados = extrair_dados_folha(sys.argv[1])
        print(json.dumps(dados, indent=2, ensure_ascii=False))
    else:
        print("Uso: python extrair_resumo_folha.py <caminho_do_pdf>")