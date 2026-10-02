"""
Confirmação de que um documento é mesmo uma NFS-e (2026-09-25).

O maestro decidia "é NFS-e" só porque a extração de NFS-e passava na
validação do schema. Teste com documentos reais mostrou que isso é
instável: para cumprir o schema, a IA INVENTA os campos que faltam numa
fatura comum (código do serviço "N/D" numa fatura, "04.22" numa conta da
Energia Exemplo, "825922" num boleto da Link Exemplo) - a mesma fatura era aceita como
NFS-e numa tentativa e rejeitada na seguinte. Documento mal roteado ganha
descrição/observação de NFS-e e a regra de banco de NFS-e, que não servem
para ele. Também confirmado: NFCom de telecom (ex: Link Exemplo Telecom) não é
NFS-e municipal - no histórico da Conta Azul esses lançamentos sempre foram
feitos como fatura ("FT ... Link Dedicado").

Duas checagens, propositalmente independentes (mesmo padrão de
deteccao_injecao.py):
1. Texto do PDF, SEM IA: NFS-e de verdade traz no texto "NFS-e", "Nota
   Fiscal de Serviço(s) Eletrônica" ou o par prestador + tomador. Sem
   nenhuma dessas marcas num PDF que tem texto, não é NFS-e.
2. A própria extração declara o tipo do documento
   ("tipo_documento_detectado") - vale sozinha quando o PDF não tem camada
   de texto (escaneado/imagem).
"""

from __future__ import annotations

import re

from src.core import config
from src.utils.deteccao_injecao import extrair_texto_pdf, normalizar_texto

logger = config.get_logger(__name__)

# Abaixo disso o PDF é considerado sem camada de texto (escaneado/imagem):
# a checagem de texto não opina e vale só a declaração da IA.
_MINIMO_CARACTERES_TEXTO = 100

_MARCAS_NFSE = [
    re.compile(r"\bnfs-?e\b"),
    re.compile(r"nota fiscal (eletronica )?de servicos?\W{0,3}(eletronica)?\W{0,3}nfs"),
    re.compile(r"nota fiscal de servicos? eletronica"),
    re.compile(r"nota fiscal eletronica de servicos?"),
]
_PRESTADOR = re.compile(r"\bprestador")
_TOMADOR = re.compile(r"\btomador")

TIPO_NFSE = "nfse"


def texto_tem_marca_de_nfse(texto: str) -> bool | None:
    """True/False conforme o texto do PDF tenha marca de NFS-e; None quando
    não há texto suficiente para opinar (PDF escaneado ou imagem)."""
    if not texto or len(texto.strip()) < _MINIMO_CARACTERES_TEXTO:
        return None
    normalizado = normalizar_texto(texto)
    if any(marca.search(normalizado) for marca in _MARCAS_NFSE):
        return True
    return bool(_PRESTADOR.search(normalizado) and _TOMADOR.search(normalizado))


def motivo_para_nao_ser_nfse(dados: dict | None, caminho_documento: str | None) -> str | None:
    """None quando o documento pode seguir como NFS-e; senão, o motivo
    (para log) de ele ir para o fluxo de documento comum."""
    tipo_declarado = (dados or {}).get("tipo_documento_detectado")
    if tipo_declarado and str(tipo_declarado).strip().lower() != TIPO_NFSE:
        return f"a extração identificou o documento como '{tipo_declarado}', não como NFS-e"
    if caminho_documento:
        tem_marca = texto_tem_marca_de_nfse(extrair_texto_pdf(caminho_documento))
        if tem_marca is False:
            return "o texto do PDF não tem nenhuma marca de NFS-e (\"NFS-e\", \"Nota Fiscal de Serviço Eletrônica\" ou prestador + tomador)"
    return None
