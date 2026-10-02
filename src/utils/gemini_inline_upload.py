"""
Envio inline (bytes) de documentos pro Gemini - substitui, nos fluxos de
extração de folha de pagamento e de documento comum, o upload por arquivo
feito em `gemini_upload_helper.py`.

POR QUE: o upload por arquivo (`client.files.upload`) faz o Gemini processar
o documento como texto extraído - elementos visuais (caixas coloridas,
carimbos, anotações usadas pelo financeiro para marcar centro de custo)
eram perdidos. Enviando os bytes inline com `types.Part.from_bytes` e o
`mime_type` correto, o Gemini renderiza a página nativamente (igual abrir o
PDF/imagem numa tela), sem conversão nem rasterização local.

`gemini_upload_helper.py` continua existindo (não foi alterado) para outros
usos - só não é mais chamado por `extrair_resumo_folha.py` nem
`agente_integrado.py`.
"""

from __future__ import annotations

import mimetypes

from google.genai import types


def preparar_parte_inline(caminho_documento: str) -> types.Part:
    """Lê o arquivo em bytes e devolve um `types.Part` pronto para entrar em
    `contents=[...]` de `generate_content`. O mime_type é adivinhado pela
    extensão; como este projeto só aceita .pdf/.jpg/.jpeg/.png (ver
    monitor_pasta.EXTENSOES_ACEITAS), o fallback para "application/pdf"
    cobre o caso (teórico) de uma extensão não reconhecida."""
    mime_type, _ = mimetypes.guess_type(caminho_documento)
    mime_type = mime_type or "application/pdf"

    with open(caminho_documento, "rb") as f:
        dados = f.read()

    return types.Part.from_bytes(data=dados, mime_type=mime_type)
