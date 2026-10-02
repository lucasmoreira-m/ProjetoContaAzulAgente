"""Confirmação de NFS-e antes de rotear (2026-09-25) - ver src/utils/classificacao_documento.py.
Textos abaixo imitam trechos reais dos documentos usados no diagnóstico."""

from src.utils import classificacao_documento as cd

TEXTO_NFSE_SAUDE = (
    "PREFEITURA DO MUNICÍPIO DE BARUERI - NOTA FISCAL DE SERVIÇOS ELETRÔNICA - NFS-e "
    "Código de Verificação XYZ. PRESTADOR DE SERVIÇOS: Saúde Exemplo S.A.. "
    "TOMADOR DE SERVIÇOS: Empresa Engenharia. Valor do ISS retido."
)
TEXTO_CONTA_ENERGIA = (
    "ENERGIA EXEMPLO Companhia Energetica de Minas Gerais FATURA DE ENERGIA ELETRICA Referente a: Outubro/2026 "
    "DADOS DO CLIENTE Nome/Razao Social: Empresa GROUP CNPJ: 12.345.678/0001-90 Numero da UC 7654321-0 "
    "Data de Vencimento: 30/10/2026 Codigo de Barras 836900000014"
)
TEXTO_NFCOM_LINK = (
    "LINK EXEMPLO TELECOM LTDA - DOCUMENTO AUXILIAR DA NOTA FISCAL FATURA DE SERVIÇOS DE COMUNICAÇÃO "
    "ELETRÔNICA - NFCOM. Chave de acesso 3126... LINK EXEMPLO ADVANTAGE - Link dedicado. Linha digitável 34191..."
)


def test_nfse_de_verdade_tem_marca():
    assert cd.texto_tem_marca_de_nfse(TEXTO_NFSE_SAUDE) is True


def test_prestador_e_tomador_juntos_bastam():
    texto = "Nota de serviço. Prestador: Fulano Consultoria. Tomador: Empresa Engenharia. " * 3
    assert cd.texto_tem_marca_de_nfse(texto) is True


def test_conta_de_consumo_e_nfcom_de_telecom_nao_tem_marca():
    assert cd.texto_tem_marca_de_nfse(TEXTO_CONTA_ENERGIA) is False
    assert cd.texto_tem_marca_de_nfse(TEXTO_NFCOM_LINK) is False


def test_pdf_sem_texto_nao_opina():
    assert cd.texto_tem_marca_de_nfse("") is None
    assert cd.texto_tem_marca_de_nfse("IRPJ 3 TRIM") is None  # PDF escaneado real: 45 caracteres


def test_tipo_declarado_pela_ia_diferente_de_nfse_barra(monkeypatch):
    monkeypatch.setattr(cd, "extrair_texto_pdf", lambda caminho: TEXTO_NFSE_SAUDE)
    assert "nfcom_telecom" in cd.motivo_para_nao_ser_nfse({"tipo_documento_detectado": "nfcom_telecom"}, "x.pdf")


def test_texto_sem_marca_barra_mesmo_com_ia_dizendo_nfse(monkeypatch):
    monkeypatch.setattr(cd, "extrair_texto_pdf", lambda caminho: TEXTO_CONTA_ENERGIA)
    assert "marca de NFS-e" in cd.motivo_para_nao_ser_nfse({"tipo_documento_detectado": "nfse"}, "energia_exemplo.pdf")


def test_nfse_confirmada_ou_sem_informacao_segue(monkeypatch):
    monkeypatch.setattr(cd, "extrair_texto_pdf", lambda caminho: TEXTO_NFSE_SAUDE)
    assert cd.motivo_para_nao_ser_nfse({"tipo_documento_detectado": "nfse"}, "saude.pdf") is None
    monkeypatch.setattr(cd, "extrair_texto_pdf", lambda caminho: "")
    assert cd.motivo_para_nao_ser_nfse({}, "escaneado.pdf") is None  # sem texto e sem declaração: mantém como antes
