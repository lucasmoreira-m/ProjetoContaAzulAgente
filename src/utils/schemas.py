"""
Validação de schema dos dados extraídos pela IA (Escopo 2 e 5 da auditoria:
"validação de schema de resposta - não confiar cegamente no payload").

Antes, o código acessava dados["valor_total"], dados["fornecedor"] etc.
direto, sem checagem: se o Gemini devolvesse um JSON com formato levemente
diferente do esperado (campo faltando, tipo errado), o erro só aparecia
como um KeyError/TypeError genérico lá na frente, difícil de rastrear.

Estes modelos pydantic validam o formato assim que o JSON chega, com
mensagens de erro claras, e ainda documentam o contrato de dados esperado.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class RateioCentroCustoItem(BaseModel):
    centro_custo: str
    valor: float


class DocumentoComumExtraido(BaseModel):
    """Resultado esperado da extração de fatura/prestação de contas
    (agente_integrado.extrair_dados_documento)."""

    tipo_documento: str
    fornecedor: str
    cnpj: Optional[str] = None  # novo: chave para o registro de aprendizado (fornecedores_registry.py)
    valor_total: float
    data_vencimento: str
    numero_documento: Optional[str] = None
    categoria_sugerida: Optional[str] = None
    rateio_centro_custo_documento: list[RateioCentroCustoItem] = Field(default_factory=list)
    observacoes: Optional[str] = None
    # Diretrizes Globais de Texto (2026-09-03): sugestão de conta financeira
    # e de centro de custo (só usada quando o documento NÃO trouxer um
    # rateio_centro_custo_documento explícito - um fallback de UM centro só
    # para o valor inteiro, não uma divisão por linha) vindas do cruzamento
    # entre o texto do documento e as diretrizes aprendidas por
    # palavra-chave (ver agente_integrado.PROMPT_EXTRACAO_DOCUMENTO_COMUM e
    # src/utils/historico_centros_funcionarios.buscar_diretrizes_globais_texto).
    conta_financeira_sugerida: Optional[str] = None
    centro_custo_sugerido: Optional[str] = None
    # Quais palavras-chave das diretrizes globais (ex: "voo", "uber") a IA
    # encontrou no texto do documento - mostradas/editáveis no painel para o
    # humano confirmar, e usadas para reforçar/gravar a diretriz global
    # quando ele aprova (ver painel_revisao.py).
    palavras_chave_identificadas: list[str] = Field(default_factory=list)
    # Prompt injection (2026-09-24, ver src/utils/deteccao_injecao.py): a IA
    # marca quando o documento traz texto tentando dar ordens a ela. Optional
    # porque o modelo às vezes devolve null em vez de false.
    alerta_instrucao_no_documento: Optional[bool] = False
    trecho_suspeito: Optional[str] = None

    @field_validator("tipo_documento")
    @classmethod
    def _tipo_valido(cls, v: str) -> str:
        permitidos = {"conta_fixa", "prestacao_contas"}
        if v not in permitidos:
            raise ValueError(f"tipo_documento deve ser um de {permitidos}, veio '{v}'")
        return v

    @field_validator("valor_total")
    @classmethod
    def _valor_positivo(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"valor_total deve ser positivo, veio {v}")
        return v


class TabelaFolha(BaseModel):
    tipo: Optional[str] = None
    arquivo_bancario: Optional[str] = None
    circulada: bool = True
    rateio: list[RateioCentroCustoItem] = Field(default_factory=list)
    # Só preenchido para tipos "individuais" (ferias, pro_labore): cada
    # lançamento é de UM funcionário específico, não uma tabela consolidada
    # com vários centros de custo. O fornecedor do lançamento é essa
    # pessoa, não um fornecedor fixo como "Ministério da Fazenda" (FGTS/
    # INSS) - descoberto ao investigar o histórico real da Conta Azul.
    funcionario: Optional[str] = None
    # Data de pagamento/vencimento extraída diretamente do documento
    # (formato DD/MM/AAAA) - usada só para ferias/pro_labore, que não têm
    # uma fórmula fixa de vencimento como FGTS (dia 20 do mês seguinte) ou
    # INSS - cada recibo já traz a data específica escrita nele.
    data_pagamento_documento: Optional[str] = None
    # Só para tipo "ferias": a linha do período de gozo exatamente como
    # aparece no recibo (ex: "21/09/2026 A 05/10/2026 = 15 Dias") - usada
    # para compor a descrição do lançamento (padrão contábil validado:
    # "Férias - {periodo_gozo}"), em vez do nome do funcionário.
    periodo_gozo: Optional[str] = None
    # Título/cabeçalho ou qualquer texto de contexto próximo a ESTA tabela,
    # copiado exatamente como aparece no documento (2026-09-02) - sinal de
    # segurança independente da classificação "tipo": usado por
    # agente_folha.py para detectar quando a IA classificou por engano um
    # FGTS de RESCISÃO (GRRF) como "fgts" comum (caso real de produção -
    # aplicava a fórmula errada de mês cheio em vez da data exata do
    # documento).
    contexto_bruto: Optional[str] = None


class DocumentoFolhaExtraido(BaseModel):
    """Resultado esperado da extração de resumo de folha de pagamento
    (extrair_resumo_folha.extrair_dados_folha).

    competencia é OPCIONAL (era obrigatório antes) - documentos individuais
    por funcionário (ex: recibo de férias) não têm um campo de "competência
    da empresa toda" como os resumos consolidados de FGTS/INSS/Salários
    têm. Para esses casos, a data usada vem de
    TabelaFolha.data_pagamento_documento em vez de ser calculada a partir
    da competência."""

    empresa: Optional[str] = None
    cnpj: Optional[str] = None
    competencia: Optional[str] = None
    data_emissao: Optional[str] = None
    vencimento_documento: Optional[str] = None
    tabelas: list[TabelaFolha] = Field(default_factory=list)


class DocumentoNfseExtraido(BaseModel):
    """Resultado esperado da extração de NFS-e (Nota Fiscal de Serviço
    Eletrônica) - ver agente_nfs_e.extrair_dados_nfse.

    Nomes de campo seguem a terminologia que aparece de fato NA NOTA (não
    nomes inventados): "Código do Serviço" (não "código de tributação" -
    correção de nomenclatura) e "Discriminação dos Serviços" (não existe
    campo "descrição do serviço" separado - era erro de digitação)."""

    numero_nota: str
    # Série da nota (2026-09-08, pedido do usuário: "a chave correta é
    # usar o Número da NFS-e + Série" - valor puro repetido todo mês por
    # um mesmo fornecedor NÃO é chave confiável de identidade, ver
    # agente_nfs_e.buscar_lancamento_ja_importado).
    serie: Optional[str] = None
    tipo_servico: Optional[str] = None
    discriminacao_servicos: str
    codigo_servico: str
    descricao_codigo_servico: Optional[str] = None
    fornecedor_nome: str
    fornecedor_cnpj: Optional[str] = None
    categoria_sugerida: Optional[str] = None
    valor_total: float  # valor do serviço
    valor_imposto: Optional[float] = None
    valor_liquido: Optional[float] = None
    tem_boleto: bool = False
    boleto_data_vencimento: Optional[str] = None  # DD/MM/AAAA, como no documento
    # True quando o documento não é uma NFS-e "pura" endereçada à empresa
    # para pagamento direto ao prestador, e sim uma nota citada DENTRO de
    # uma prestação de contas/reembolso de colaborador (ex: várias notas
    # de fornecedores diferentes, ou uma capa de "relatório de despesas").
    # Ver src/agentes/maestro.py - esse caso continua no fluxo de
    # reembolso (agente_integrado, autonomia zero), nunca neste agente.
    eh_prestacao_contas_de_funcionario: bool = False
    # Rateio por centro de custo, só quando a PRÓPRIA nota trouxer uma
    # divisão explícita (2026-09-08, pedido do usuário - "às vezes precisa,
    # a própria nota mostra a divisão"; mesmo padrão de detecção flexível
    # já usado em DocumentoComumExtraido, não depende de um sinal visual
    # específico tipo "linha vermelha" continuar existindo).
    rateio_centro_custo_documento: list[RateioCentroCustoItem] = Field(default_factory=list)
    # Prompt injection (2026-09-24, ver src/utils/deteccao_injecao.py): a IA
    # marca quando o documento traz texto tentando dar ordens a ela. Optional
    # porque o modelo às vezes devolve null em vez de false.
    alerta_instrucao_no_documento: Optional[bool] = False
    trecho_suspeito: Optional[str] = None
    # Roteamento (2026-09-25, ver src/utils/classificacao_documento.py): a IA
    # declara que tipo de documento é ("nfse", "nfcom_telecom",
    # "conta_consumo", "boleto", "nota_fiscal_produto", "outro") - só "nfse"
    # segue para o agente de NFS-e. Optional: extrações antigas não têm.
    tipo_documento_detectado: Optional[str] = None

    @field_validator("valor_total")
    @classmethod
    def _valor_positivo(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"valor_total deve ser positivo, veio {v}")
        return v

    @field_validator("valor_liquido")
    @classmethod
    def _valor_liquido_obrigatorio_com_imposto(cls, v: Optional[float], info) -> Optional[float]:
        valor_imposto = info.data.get("valor_imposto")
        if valor_imposto and valor_imposto > 0 and v is None:
            raise ValueError("valor_liquido é obrigatório quando há valor_imposto > 0")
        return v

    @field_validator("boleto_data_vencimento")
    @classmethod
    def _vencimento_obrigatorio_com_boleto(cls, v: Optional[str], info) -> Optional[str]:
        if info.data.get("tem_boleto") and not v:
            raise ValueError("boleto_data_vencimento é obrigatório quando tem_boleto=True")
        return v


def validar_documento_comum(dados_brutos: dict) -> DocumentoComumExtraido:
    """Levanta pydantic.ValidationError (com mensagem clara) se o JSON da IA
    não bater com o formato esperado."""
    return DocumentoComumExtraido.model_validate(dados_brutos)


def validar_documento_folha(dados_brutos: dict) -> DocumentoFolhaExtraido:
    return DocumentoFolhaExtraido.model_validate(dados_brutos)


def validar_documento_nfse(dados_brutos: dict) -> DocumentoNfseExtraido:
    return DocumentoNfseExtraido.model_validate(dados_brutos)