"""
Regras da tela de Revisão sem depender de framework de interface
(2026-09-30, migração do Streamlit para o painel FastAPI). O Streamlit e o
FastAPI usam as mesmas funções daqui durante a migração.

Documento comum (conta fixa / prestação de contas): montar o formulário
com as sugestões, aprovar e lançar na Conta Azul, descartar. A lógica é a
mesma que existia em painel_revisao.py, com duas coisas novas, pedidas para
o uso por várias pessoas:
- trava por item: duas pessoas aprovando o mesmo item ao mesmo tempo não
  geram dois lançamentos (a segunda recebe "outra pessoa está tratando");
- quem aprovou vai para o histórico local ("aprovado_por").
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime

from filelock import FileLock, Timeout

from src.core import config
from src.core import conta_azul_client as ca
from src.core.conta_azul_auth import obter_access_token
from src.core.conta_azul_client import ContaAzulAPIError
from src.agentes import agente_folha
from src.agentes import agente_integrado
from src.agentes import agente_nfs_e
from src.agentes.agente_integrado import _match_centro_custo, converter_data, criar_lancamento, normalizar
from src.utils import fornecedores_registry
from src.utils import historico_centros_funcionarios
from src.utils import historico_lancamentos
from src.utils import regras_nfse
from src.utils import verificacao_duplicidade
from src.utils.formato_texto import data_para_texto_br
from src.utils.pastas_trabalho import listar_pendencias
from src.utils.regras_folha_pagamento import CENTRO_CUSTO_PLACEHOLDER

logger = config.get_logger(__name__)

NOME_TIPO_DOCUMENTO_LEGIVEL = {
    "conta_fixa": "Conta Fixa (fatura de um único fornecedor)",
    "prestacao_contas": "Prestação de Contas / Reembolso",
}


# ---------------------------------------------------------------------------
# Tipo de cada pendência
# ---------------------------------------------------------------------------
def eh_arquivo_duplicado(dados: dict) -> bool:
    """Camada 1 de duplicidade (hash do arquivo) bloqueou o reprocessamento
    ANTES de qualquer extração: `dados` só tem "registro_anterior", sem
    "tipo_documento" (ver monitor_pasta.processar_documento_com_classificacao).
    Sem esta checagem o item caía por engano na tela de folha."""
    return isinstance(dados, dict) and "registro_anterior" in dados and "tipo_documento" not in dados


def eh_documento_nfse(dados: dict) -> bool:
    return isinstance(dados, dict) and dados.get("tipo_documento") == "nfse"


def eh_documento_comum(dados: dict) -> bool:
    """Formato 'achatado' (fatura/prestação de contas), diferente da folha
    (dict de resultados por tabela). NFS-e também é achatado, mas tem tela
    própria - checar eh_documento_nfse antes."""
    return isinstance(dados, dict) and "tipo_documento" in dados and dados.get("tipo_documento") != "nfse"


def tipo_pendencia(dados) -> str:
    """'duplicado', 'nfse', 'comum' ou 'folha' - mesma ordem de decisão da
    tela de revisão."""
    if eh_arquivo_duplicado(dados):
        return "duplicado"
    if eh_documento_nfse(dados):
        return "nfse"
    if eh_documento_comum(dados):
        return "comum"
    return "folha"


# ---------------------------------------------------------------------------
# Cadastros da Conta Azul (cache de 2 minutos, como o painel antigo)
# ---------------------------------------------------------------------------
TTL_CADASTROS_SEGUNDOS = 120
_trava_cadastros = threading.Lock()
_cache_cadastros: dict = {"quando": 0.0, "dados": None}


def carregar_cadastros(forcar: bool = False) -> dict:
    """Categorias, pessoas, centros de custo e contas financeiras. Só é
    chamado ao abrir um item para revisar (lazy loading: listar pendências
    não gasta o refresh_token)."""
    with _trava_cadastros:
        agora = time.monotonic()
        if not forcar and _cache_cadastros["dados"] and agora - _cache_cadastros["quando"] < TTL_CADASTROS_SEGUNDOS:
            return _cache_cadastros["dados"]
        token = obter_access_token()
        dados = {
            "categorias": sorted(ca.get_todas_paginas(token, "/v1/categorias", chave_itens="itens"), key=lambda c: c["nome"]),
            "pessoas": sorted(ca.get_todas_paginas(token, "/v1/pessoas", chave_itens="items"), key=lambda p: p["nome"]),
            "centros_custo": sorted(ca.get_todas_paginas(token, "/v1/centro-de-custo", chave_itens="itens"), key=lambda c: c["nome"]),
            "contas_financeiras": sorted(ca.get_todas_paginas(token, "/v1/conta-financeira", chave_itens="itens"), key=lambda c: c["nome"]),
        }
        _cache_cadastros.update(quando=agora, dados=dados)
        return dados


def limpar_cache_cadastros() -> None:
    with _trava_cadastros:
        _cache_cadastros.update(quando=0.0, dados=None)


# ---------------------------------------------------------------------------
# Localizar e travar um item
# ---------------------------------------------------------------------------
class ItemEmUso(Exception):
    """Outra pessoa está aprovando/descartando este item agora."""


def caminho_json_do_item(item_id: str) -> str | None:
    """item_id = nome do _dados.json sem o sufixo. Só aceita nome simples
    dentro de revisao/ (nada de caminho vindo do navegador)."""
    if not item_id or item_id != os.path.basename(item_id) or item_id.startswith("."):
        return None
    caminho = os.path.join(config.PASTA_REVISAO, f"{item_id}_dados.json")
    return caminho if os.path.isfile(caminho) else None


def id_do_item(caminho_json: str) -> str:
    return os.path.basename(caminho_json)[: -len("_dados.json")]


def carregar_item(item_id: str) -> dict | None:
    caminho_json = caminho_json_do_item(item_id)
    if caminho_json is None:
        return None
    return next((p for p in listar_pendencias() if p["caminho_json"] == caminho_json), None)


def _trava_do_item(caminho_json: str) -> FileLock:
    if config.PASTA_TRAVAS:
        return FileLock(config.caminho_trava(caminho_json), timeout=0)
    pasta = os.path.join(config.PASTA_REVISAO, ".travas")
    os.makedirs(pasta, exist_ok=True)
    return FileLock(os.path.join(pasta, os.path.basename(caminho_json) + ".lock"), timeout=0)


# ---------------------------------------------------------------------------
# Formulário do documento comum
# ---------------------------------------------------------------------------
def parse_valor(texto) -> float:
    """'1.234,56', '1234,56' ou '1234.56' -> 1234.56."""
    texto = str(texto or "").strip().replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    return float(texto)


def _indice_contendo(sugestao: str, nomes: list[str], exigir_sugestao: bool = False) -> int:
    """Mesma regra do painel antigo: primeiro nome que CONTÉM a sugestão
    (normalizada), senão o primeiro da lista."""
    if exigir_sugestao and not sugestao:
        return 0
    alvo = normalizar(sugestao)
    return next((i for i, n in enumerate(nomes) if alvo in normalizar(n)), 0)


def status_palavras_chave(texto_palavras: str) -> list[dict]:
    """Origem de cada palavra-chave: nova, confirmada por humano ou inferida
    do histórico (nunca revisada - pede atenção)."""
    conhecidas = historico_centros_funcionarios.buscar_diretrizes_globais_texto()
    linhas = []
    for palavra in [p.strip() for p in (texto_palavras or "").split(",") if p.strip()]:
        entrada = conhecidas.get(normalizar(palavra))
        if not entrada:
            linhas.append({"tipo": "nova", "palavra": palavra, "texto": "nova, ainda sem diretriz gravada."})
        elif entrada.get("origem") == "aprovado_humano":
            linhas.append({"tipo": "humano", "palavra": palavra, "texto": (
                f"confirmada por humano no painel {entrada.get('vezes_confirmada', 0)}x "
                f"(categoria: {entrada.get('categoria_nome') or '-'})."
            )})
        else:
            linhas.append({"tipo": "inferida", "palavra": palavra, "texto": (
                f"inferida automaticamente do histórico ({entrada.get('vezes_confirmada', 0)} lançamentos "
                f"antigos, nenhuma confirmação humana) - categoria sugerida: {entrada.get('categoria_nome') or '-'}. "
                "Confira com atenção."
            )})
    return linhas


def partes_do_motivo(motivo: str | None) -> list[dict]:
    """Separa as medidas de segurança (teto de valor, suspeita de documento
    manipulado) do resto do motivo, que os agentes juntam com " | "."""
    partes = [p.strip() for p in (motivo or "").split(" | ") if p.strip()]
    saida, outros = [], []
    for parte in partes:
        if parte.startswith("MEDIDA DE SEGURANÇA"):
            saida.append({"tipo": "seguranca", "titulo": "Medida de segurança — valor fora do padrão recorrente",
                          "texto": parte.split(":", 1)[-1].strip()})
        elif parte.startswith("POSSÍVEL MANIPULAÇÃO DO DOCUMENTO"):
            saida.append({"tipo": "manipulacao", "titulo": "Possível manipulação do documento",
                          "texto": parte.split(":", 1)[-1].strip()})
        else:
            outros.append(parte)
    if outros or not partes:
        saida.append({"tipo": "motivo", "titulo": None, "texto": " | ".join(outros) or "Pendente de revisão."})
    return saida


def formulario_comum(dados: dict, cadastros: dict, enviado: dict | None = None) -> dict:
    """Valores do formulário: as sugestões da extração (primeira abertura)
    ou o que a pessoa já tinha preenchido (`enviado`, ao reabrir depois de
    um aviso de duplicata ou erro)."""
    categorias = cadastros["categorias"]
    pessoas = cadastros["pessoas"]
    centros = cadastros["centros_custo"]
    contas = cadastros["contas_financeiras"]

    def _id_padrao(lista, indice):
        return lista[indice]["id"] if lista else ""

    padrao = {
        "valor": f"{float(dados.get('valor_total') or 0):.2f}".replace(".", ","),
        "data_vencimento": data_para_texto_br(dados.get("data_vencimento")),
        "categoria_id": _id_padrao(categorias, _indice_contendo(dados.get("categoria_sugerida") or "", [c["nome"] for c in categorias])),
        "contato_id": _id_padrao(pessoas, _indice_contendo(dados.get("fornecedor") or "", [p["nome"] for p in pessoas])),
        "conta_financeira_id": _id_padrao(contas, _indice_contendo(
            dados.get("conta_financeira_sugerida") or "", [c["nome"] for c in contas], exigir_sugestao=True)),
        "palavras_chave": ", ".join(dados.get("palavras_chave_identificadas") or []),
        "orientacao": "",
    }

    rateio_doc = dados.get("rateio_centro_custo_documento") or []
    linhas_rateio = []
    for i, linha in enumerate(rateio_doc):
        nome_doc = linha.get("centro_custo", "")
        centro_match, tipo_match = _match_centro_custo(nome_doc, centros)
        linhas_rateio.append({
            "i": i,
            "nome_doc": nome_doc,
            "tipo_match": tipo_match,
            "centro_id": (centro_match or (centros[0] if centros else {})).get("id", ""),
            "valor": f"{float(linha.get('valor') or 0):.2f}".replace(".", ","),
            "incluir": tipo_match == "exato",
        })

    mostrar_fallback = not rateio_doc and bool(centros)
    centro_fallback_id = ""
    if mostrar_fallback:
        sugestao = dados.get("centro_custo_sugerido") or ""
        if sugestao:
            achado = next((c for c in centros if normalizar(sugestao) in normalizar(c["nome"])), None)
            centro_fallback_id = achado["id"] if achado else ""

    valores = dict(padrao, centro_fallback_id=centro_fallback_id)
    if enviado is not None:
        for campo in valores:
            valores[campo] = enviado.get(campo, "")
        for linha in linhas_rateio:
            linha["incluir"] = enviado.get(f"rateio_{linha['i']}_incluir") == "on"
            linha["centro_id"] = enviado.get(f"rateio_{linha['i']}_centro_id", linha["centro_id"])
            linha["valor"] = enviado.get(f"rateio_{linha['i']}_valor", linha["valor"])

    return {
        "valores": valores,
        "linhas_rateio": linhas_rateio,
        "mostrar_fallback": mostrar_fallback,
        "categorias": categorias,
        "pessoas": pessoas,
        "centros_custo": centros,
        "contas_financeiras": contas,
        "status_palavras": status_palavras_chave(valores["palavras_chave"]),
        "tipo_legivel": NOME_TIPO_DOCUMENTO_LEGIVEL.get(dados.get("tipo_documento"), dados.get("tipo_documento") or "?"),
        "fornecedor": dados.get("fornecedor"),
        "origem_categoria": dados.get("origem_categoria"),
        "camada3": dados.get("camada_decisao") == "camada3_fallback_ia",
    }


# ---------------------------------------------------------------------------
# Aprovar / descartar documento comum
# ---------------------------------------------------------------------------
def _por_id(lista: list[dict], item_id: str) -> dict | None:
    return next((x for x in lista if x["id"] == item_id), None) if item_id else None


def _rateio_do_formulario(dados: dict, form: dict, valor: float, centros: list[dict]) -> list[dict] | None:
    rateio_doc = dados.get("rateio_centro_custo_documento") or []
    linhas = []
    for i in range(len(rateio_doc)):
        if form.get(f"rateio_{i}_incluir") != "on":
            continue
        valor_linha = round(parse_valor(form.get(f"rateio_{i}_valor")), 2)
        # A Conta Azul recusa (400) rateio com valor zero.
        if valor_linha <= 0:
            continue
        centro = _por_id(centros, form.get(f"rateio_{i}_centro_id", ""))
        if centro is None:
            raise ValueError(f"Centro de custo da linha {i + 1} do rateio não encontrado.")
        linhas.append({"centro_custo_id": centro["id"], "centro_custo_nome": centro["nome"], "percentual": None, "valor": valor_linha})
    if not rateio_doc and form.get("centro_fallback_id"):
        centro = _por_id(centros, form["centro_fallback_id"])
        if centro is None:
            raise ValueError("Centro de custo escolhido não encontrado.")
        linhas.append({"centro_custo_id": centro["id"], "centro_custo_nome": centro["nome"], "percentual": None, "valor": round(valor, 2)})
    return linhas or None


def _reforcar_diretrizes_globais_texto(palavras_texto: str, categoria_nome, conta_financeira_nome, rateio) -> None:
    """Cada palavra-chave confirmada reforça a diretriz global com os
    valores FINAIS aprovados. Centro de custo só com exatamente UMA linha de
    rateio (inequívoco)."""
    centro_unico = rateio[0]["centro_custo_nome"] if rateio and len(rateio) == 1 else None
    for palavra in [p.strip() for p in (palavras_texto or "").split(",") if p.strip()]:
        historico_centros_funcionarios.registrar_diretriz_global_texto(
            palavra, categoria_nome=categoria_nome, conta_financeira_nome=conta_financeira_nome,
            centro_custo_nome=centro_unico,
        )


def aprovar_comum(item_id: str, form: dict, aprovado_por: str, cadastros: dict, forcar_duplicata: bool = False) -> dict:
    """Lança na Conta Azul o documento comum revisado. Devolve
    {"status": "lancado"|"duplicata"|"ja_tratado"|"erro", ...}."""
    caminho_json = caminho_json_do_item(item_id)
    if caminho_json is None:
        return {"status": "ja_tratado"}
    try:
        with _trava_do_item(caminho_json):
            return _aprovar_comum_travado(caminho_json, form, aprovado_por, cadastros, forcar_duplicata)
    except Timeout:
        raise ItemEmUso() from None


def _aprovar_comum_travado(caminho_json, form, aprovado_por, cadastros, forcar_duplicata) -> dict:
    # Pode ter sido aprovado por outra pessoa enquanto esta esperava.
    item = next((p for p in listar_pendencias() if p["caminho_json"] == caminho_json), None)
    if item is None:
        return {"status": "ja_tratado"}
    dados = item["info"]["dados"]
    doc = item["caminho_documento"]
    if tipo_pendencia(dados) != "comum":
        return {"status": "erro", "mensagem": "Este item não é um documento comum."}

    categoria = _por_id(cadastros["categorias"], form.get("categoria_id", ""))
    contato = _por_id(cadastros["pessoas"], form.get("contato_id", ""))
    conta_fin = _por_id(cadastros["contas_financeiras"], form.get("conta_financeira_id", ""))
    if categoria is None or contato is None:
        return {"status": "erro", "mensagem": "Escolha a categoria e o contato."}
    try:
        valor = parse_valor(form.get("valor"))
        data_venc_iso = converter_data(form.get("data_vencimento") or "")
        rateio = _rateio_do_formulario(dados, form, valor, cadastros["centros_custo"])
    except ValueError as e:
        return {"status": "erro", "mensagem": str(e) or "Valor inválido."}

    dados_lancamento = dict(dados)
    dados_lancamento["valor_total"] = valor

    try:
        token = obter_access_token()
        if not forcar_duplicata:
            duplicata = verificacao_duplicidade.verificar_lancamento_similar_existe(
                token, valor, data_venc_iso,
                categoria_nome=categoria["nome"], contato_nome=contato["nome"],
                categoria_id=categoria["id"], contato_id=contato["id"],
            )
            if duplicata:
                return {"status": "duplicata", "duplicata": {"id": duplicata.get("id"), "status": duplicata.get("status")}}

        status, resposta = criar_lancamento(
            token, dados_lancamento, categoria["id"], contato["id"], data_venc_iso,
            rateio_centro_custo=rateio, conta_financeira_id=conta_fin["id"] if conta_fin else None,
        )
    except ContaAzulAPIError as e:
        return {"status": "erro", "mensagem": f"Erro de comunicação com a Conta Azul: {e}"}
    if status != 200:
        logger.error("Erro %s ao lançar via painel: %s", status, resposta)
        return {"status": "erro", "mensagem": "Erro ao lançar. Veja os logs para detalhes."}

    logger.info("Documento comum %s lançado por %s (protocolo %s).", os.path.basename(caminho_json), aprovado_por, resposta.get("protocolo"))
    fornecedores_registry.registrar_confirmado(
        cnpj=dados.get("cnpj"), nome=dados.get("fornecedor") or contato["nome"],
        contato_id=contato["id"], contato_nome=contato["nome"],
        categoria_id=categoria["id"], categoria_nome=categoria["nome"], origem="aprovado_humano",
    )
    _reforcar_diretrizes_globais_texto(form.get("palavras_chave"), categoria["nome"], conta_fin["nome"] if conta_fin else None, rateio)
    orientacao = (form.get("orientacao") or "").strip()
    historico_centros_funcionarios.registrar_diretriz_livre("comum", orientacao)
    reextraidos = reextrair_pendentes_com_nova_diretriz("comum", caminho_json) if orientacao else 0

    _promover_para_processados(caminho_json, doc, {
        "protocolo": resposta.get("protocolo"),
        "descricao": dados_lancamento.get("descricao"),
        "observacoes": dados_lancamento.get("observacoes"),
        "valor": valor,
        "data_vencimento": data_venc_iso,
        "data_competencia": data_venc_iso,
        "categoria": {"id": categoria["id"], "nome": categoria["nome"]},
        "contato": {"id": contato["id"], "nome": contato["nome"]},
        "rateio_aplicado": rateio,
        "resposta_criacao": resposta,
        "origem_categoria": dados.get("origem_categoria"),
        "camada_decisao": dados.get("camada_decisao"),
        "camada2_evidencia_qtd": dados.get("camada2_evidencia_qtd"),
        "aprovado_por": aprovado_por,
    })
    return {"status": "lancado", "protocolo": resposta.get("protocolo"), "reextraidos": reextraidos}


def descartar(item_id: str, descartado_por: str) -> bool:
    """Move documento e _dados.json para descartados/. False se o item já
    não estava mais na revisão."""
    caminho_json = caminho_json_do_item(item_id)
    if caminho_json is None:
        return False
    try:
        with _trava_do_item(caminho_json):
            item = next((p for p in listar_pendencias() if p["caminho_json"] == caminho_json), None)
            if item is None:
                return False
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            doc = item["caminho_documento"]
            if doc:
                shutil.move(doc, os.path.join(config.PASTA_DESCARTADOS, f"{timestamp}_{os.path.basename(doc)}"))
            shutil.move(caminho_json, os.path.join(config.PASTA_DESCARTADOS, f"{timestamp}_dados.json"))
    except Timeout:
        raise ItemEmUso() from None
    logger.info("Item %s descartado por %s.", item_id, descartado_por)
    return True


# ---------------------------------------------------------------------------
# Peças comuns a NFS-e e folha
# ---------------------------------------------------------------------------
def _match_local(nome_sugerido: str | None, itens: list[dict]) -> dict | None:
    """Match local contra a lista já carregada: nome exato, depois um
    contido no outro (mesma regra da tela de NFS-e do painel antigo)."""
    if not nome_sugerido:
        return None
    alvo = normalizar(nome_sugerido)
    for it in itens:
        if normalizar(it["nome"]) == alvo:
            return it
    for it in itens:
        if alvo in normalizar(it["nome"]) or normalizar(it["nome"]) in alvo:
            return it
    return None


def _valor_br(numero) -> str:
    return f"{float(numero or 0):.2f}".replace(".", ",")


def _linhas_rateio(rateio_doc: list[dict], centros: list[dict], enviado: dict | None, prefixo: str = "rateio") -> list[dict]:
    """Linhas do editor de rateio: centro pré-selecionado pelo mesmo match do
    fluxo automático; só vem marcada quando o match é exato."""
    linhas = []
    for i, linha in enumerate(rateio_doc):
        nome_doc = linha.get("centro_custo") or ""
        centro_match, tipo_match = _match_centro_custo(nome_doc, centros)
        item = {
            "i": i,
            "nome_doc": nome_doc,
            "tipo_match": tipo_match,
            "centro_id": (centro_match or (centros[0] if centros else {})).get("id", ""),
            "valor": _valor_br(linha.get("valor")),
            "incluir": tipo_match == "exato",
            "aviso": (
                "Não encontrado no histórico — selecione manualmente" if nome_doc == CENTRO_CUSTO_PLACEHOLDER
                else "Nenhum centro de custo identificado — selecione manualmente" if not nome_doc else None
            ),
        }
        if enviado is not None:
            item["incluir"] = enviado.get(f"{prefixo}_{i}_incluir") == "on"
            item["centro_id"] = enviado.get(f"{prefixo}_{i}_centro_id", item["centro_id"])
            item["valor"] = enviado.get(f"{prefixo}_{i}_valor", item["valor"])
        linhas.append(item)
    return linhas


def _rateio_das_linhas(qtd_linhas: int, form: dict, centros: list[dict], prefixo: str = "rateio") -> list[dict] | None:
    linhas = []
    for i in range(qtd_linhas):
        if form.get(f"{prefixo}_{i}_incluir") != "on":
            continue
        valor_linha = round(parse_valor(form.get(f"{prefixo}_{i}_valor")), 2)
        if valor_linha <= 0:  # a Conta Azul recusa (400) rateio com valor zero
            continue
        centro = _por_id(centros, form.get(f"{prefixo}_{i}_centro_id", ""))
        if centro is None:
            raise ValueError(f"Centro de custo da linha {i + 1} do rateio não encontrado.")
        linhas.append({"centro_custo_id": centro["id"], "centro_custo_nome": centro["nome"], "percentual": None, "valor": valor_linha})
    return linhas or None


def _data_opcional(texto: str | None) -> str | None:
    texto = (texto or "").strip()
    return converter_data(texto) if texto else None


def _promover_para_processados(caminho_json: str, doc: str | None, detalhes_historico) -> None:
    """Documento resolvido: PDF e relatório vão para processados/, o
    histórico local ganha o(s) _lancamento.json e o _dados.json sai da fila."""
    if doc:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino_doc = os.path.join(config.PASTA_PROCESSADOS, f"{timestamp}_{os.path.basename(doc)}")
        shutil.move(doc, destino_doc)
        relatorio = caminho_json.replace("_dados.json", "_relatorio.txt")
        if os.path.exists(relatorio):
            shutil.move(relatorio, f"{os.path.splitext(destino_doc)[0]}_relatorio.txt")
        historico_lancamentos.registrar_lancamentos_de_resultado(destino_doc, "manual", detalhes_historico)
    os.remove(caminho_json)


def _com_trava(item_id: str, funcao, *args) -> dict:
    caminho_json = caminho_json_do_item(item_id)
    if caminho_json is None:
        return {"status": "ja_tratado"}
    try:
        with _trava_do_item(caminho_json):
            item = next((p for p in listar_pendencias() if p["caminho_json"] == caminho_json), None)
            if item is None:
                return {"status": "ja_tratado"}
            return funcao(item, *args)
    except Timeout:
        raise ItemEmUso() from None


def _checar_duplicata(token, valor, data_iso, categoria, contato) -> dict | None:
    duplicata = verificacao_duplicidade.verificar_lancamento_similar_existe(
        token, valor, data_iso,
        categoria_nome=categoria["nome"], contato_nome=contato["nome"],
        categoria_id=categoria["id"], contato_id=contato["id"],
    )
    return {"id": duplicata.get("id"), "status": duplicata.get("status")} if duplicata else None


# ---------------------------------------------------------------------------
# NFS-e
# ---------------------------------------------------------------------------
def _banco_da_nfse(dados: dict) -> tuple[str, str | None]:
    """('boleto', 'alfa') | ('reconhecido', banco) | ('escolher', None).
    Com boleto o pagamento é sempre Banco Alfa; sem boleto, o funil de banco pode
    ter reconhecido o fornecedor (camadas 1/2); senão, o revisor escolhe."""
    if dados.get("tem_boleto"):
        return "boleto", "alfa"
    if dados.get("banco_sugerido") in ("camada1_registro_local", "camada2_erp_inferido"):
        return "reconhecido", "alfa" if dados.get("conta_financeira_id") == regras_nfse.CONTA_FINANCEIRA_ALFA_ID else "beta"
    return "escolher", None


def formulario_nfse(dados: dict, cadastros: dict, enviado: dict | None = None) -> dict:
    categorias, pessoas, centros = cadastros["categorias"], cadastros["pessoas"], cadastros["centros_custo"]
    categoria_atual = (dados.get("categoria") or {}).get("nome") or dados.get("categoria_sugerida")
    contato_atual = (dados.get("contato") or {}).get("nome") or dados.get("fornecedor_nome")
    cat_pre = _match_local(categoria_atual, categorias) or (categorias[0] if categorias else {})
    cont_pre = _match_local(contato_atual, pessoas) or (pessoas[0] if pessoas else {})
    modo_banco, banco = _banco_da_nfse(dados)

    valores = {
        "valor": _valor_br(dados.get("valor_total")),
        "data_vencimento": data_para_texto_br(dados.get("data_vencimento_sugerida")),
        "data_competencia": data_para_texto_br(dados.get("data_competencia")),
        # Editável: o revisor vê e corrige o texto final que vai para a Conta Azul.
        "descricao": dados.get("descricao") or regras_nfse.montar_descricao_nfse(
            dados.get("numero_nota"), dados.get("tipo_servico"), dados.get("discriminacao_servicos"),
            dados.get("data_vencimento_sugerida") or dados.get("boleto_data_vencimento"),
        ),
        "valor_liquido": _valor_br(dados.get("valor_liquido")) if dados.get("valor_imposto") else "",
        "categoria_id": cat_pre.get("id", ""),
        "contato_id": cont_pre.get("id", ""),
        "banco": banco or "alfa",
        "orientacao": "",
    }
    if enviado is not None:
        for campo in valores:
            valores[campo] = enviado.get(campo, valores[campo] if campo == "banco" else "")

    return {
        "valores": valores,
        "linhas_rateio": _linhas_rateio(dados.get("rateio_centro_custo_documento") or [], centros, enviado),
        "categorias": categorias,
        "pessoas": pessoas,
        "centros_custo": centros,
        "modo_banco": modo_banco,
        "banco_reconhecido": "Banco Alfa" if banco == "alfa" else "Beta",
        "valor_imposto": dados.get("valor_imposto"),
        "numero_nota": dados.get("numero_nota"),
        "fornecedor": dados.get("fornecedor_nome"),
        "discriminacao": dados.get("discriminacao_servicos"),
        "codigo_servico": dados.get("codigo_servico"),
        "descricao_codigo_servico": dados.get("descricao_codigo_servico"),
    }


def aprovar_nfse(item_id: str, form: dict, aprovado_por: str, cadastros: dict, forcar_duplicata: bool = False) -> dict:
    return _com_trava(item_id, _aprovar_nfse_travado, form, aprovado_por, cadastros, forcar_duplicata)


def _aprovar_nfse_travado(item, form, aprovado_por, cadastros, forcar_duplicata) -> dict:
    caminho_json, doc = item["caminho_json"], item["caminho_documento"]
    dados = item["info"]["dados"]
    if tipo_pendencia(dados) != "nfse":
        return {"status": "erro", "mensagem": "Este item não é uma NFS-e."}

    categoria = _por_id(cadastros["categorias"], form.get("categoria_id", ""))
    contato = _por_id(cadastros["pessoas"], form.get("contato_id", ""))
    if categoria is None or contato is None:
        return {"status": "erro", "mensagem": "Escolha a categoria e o contato."}
    modo_banco, banco = _banco_da_nfse(dados)
    if modo_banco == "escolher":
        banco = form.get("banco")
        if banco not in ("alfa", "beta"):
            return {"status": "erro", "mensagem": "Escolha o banco de pagamento."}
    tem_boleto = modo_banco == "boleto"
    try:
        valor = parse_valor(form.get("valor"))
        data_venc_iso = converter_data(form.get("data_vencimento") or "")
        data_comp_iso = _data_opcional(form.get("data_competencia"))
        valor_liquido = parse_valor(form.get("valor_liquido")) if dados.get("valor_imposto") else None
        rateio = _rateio_das_linhas(len(dados.get("rateio_centro_custo_documento") or []), form, cadastros["centros_custo"])
    except ValueError as e:
        return {"status": "erro", "mensagem": str(e) or "Valor inválido."}

    dados_lancamento = dict(dados)
    dados_lancamento["valor_total"] = valor
    dados_lancamento["descricao"] = (form.get("descricao") or "").strip() or dados.get("descricao")
    conta_financeira_id = regras_nfse.CONTA_FINANCEIRA_ALFA_ID if banco == "alfa" else regras_nfse.CONTA_FINANCEIRA_BETA_ID

    try:
        token = obter_access_token()
        if not forcar_duplicata:
            duplicata = _checar_duplicata(token, valor, data_venc_iso, categoria, contato)
            if duplicata:
                return {"status": "duplicata", "duplicata": duplicata}
        status, resposta = criar_lancamento(
            token, dados_lancamento, categoria["id"], contato["id"], data_venc_iso,
            rateio_centro_custo=rateio, data_competencia=data_comp_iso,
            forma_pagamento="boleto bancário" if tem_boleto else "transferência bancária",
            conta_financeira_id=conta_financeira_id, valor_liquido=valor_liquido,
        )
    except ContaAzulAPIError as e:
        return {"status": "erro", "mensagem": f"Erro de comunicação com a Conta Azul: {e}"}
    if status != 200:
        logger.error("Erro %s ao lançar NFS-e via painel: %s", status, resposta)
        return {"status": "erro", "mensagem": "Erro ao lançar. Veja os logs para detalhes."}

    logger.info("NFS-e %s lançada por %s (protocolo %s).", os.path.basename(caminho_json), aprovado_por, resposta.get("protocolo"))
    fornecedores_registry.registrar_confirmado(
        cnpj=dados.get("fornecedor_cnpj"), nome=dados.get("fornecedor_nome") or contato["nome"],
        contato_id=contato["id"], contato_nome=contato["nome"],
        categoria_id=categoria["id"], categoria_nome=categoria["nome"], origem="aprovado_humano",
    )
    if not tem_boleto:
        # A escolha do banco vira a base para as próximas notas deste fornecedor.
        fornecedores_registry.registrar_banco_pagamento_sem_boleto(dados.get("fornecedor_cnpj"), banco, origem="aprovado_humano")
    orientacao = (form.get("orientacao") or "").strip()
    historico_centros_funcionarios.registrar_diretriz_livre("nfse", orientacao)
    reextraidos = reextrair_pendentes_com_nova_diretriz("nfse", caminho_json) if orientacao else 0

    _promover_para_processados(caminho_json, doc, {
        "protocolo": resposta.get("protocolo"),
        "descricao": dados_lancamento.get("descricao"),
        "observacoes": dados_lancamento.get("observacoes"),
        "valor": valor,
        "data_vencimento": data_venc_iso,
        "data_competencia": data_comp_iso or data_venc_iso,
        "categoria": {"id": categoria["id"], "nome": categoria["nome"]},
        "contato": {"id": contato["id"], "nome": contato["nome"]},
        "rateio_aplicado": rateio,
        "conta_financeira_id": conta_financeira_id,
        "resposta_criacao": resposta,
        "aprovado_por": aprovado_por,
    })
    return {"status": "lancado", "protocolo": resposta.get("protocolo"), "reextraidos": reextraidos}


# ---------------------------------------------------------------------------
# Folha de pagamento (uma aprovação por tabela: FGTS, INSS, Salários...)
# ---------------------------------------------------------------------------
def _rateio_doc_tabela(info_edicao: dict) -> list[dict]:
    """Sem rateio no documento, uma linha só com o valor total e sem centro
    escolhido - para o revisor ainda poder escolher um e aprovar."""
    return info_edicao.get("rateio_doc") or [{"centro_custo": "", "valor": info_edicao.get("valor_total")}]


def formulario_folha(dados: dict, cadastros: dict, tabela_enviada: str | None = None, enviado: dict | None = None) -> dict:
    categorias, pessoas = cadastros["categorias"], cadastros["pessoas"]
    centros, contas = cadastros["centros_custo"], cadastros["contas_financeiras"]
    tabelas = []
    for chave, resultado in dados.items():
        titulo = (resultado.get("detalhes") or {}).get("tipo") or chave
        titulo = titulo.upper() if isinstance(titulo, str) else str(titulo)
        if resultado.get("sucesso"):
            tabelas.append({"chave": chave, "titulo": titulo, "lancada": True,
                            "protocolo": (resultado.get("detalhes") or {}).get("protocolo")})
            continue
        info = resultado.get("dados_para_edicao") or {}
        valores = {
            "valor": _valor_br(info.get("valor_total")),
            "data_vencimento": data_para_texto_br(info.get("data_vencimento_sugerida")),
            "data_competencia": data_para_texto_br(info.get("data_competencia_sugerida")),
            "descricao": info.get("descricao_sugerida") or "",
            "categoria_id": categorias[_indice_contendo(info.get("categoria_nome_sugerida") or "", [c["nome"] for c in categorias])]["id"] if categorias else "",
            "contato_id": pessoas[_indice_contendo(info.get("fornecedor_nome_sugerido") or "", [p["nome"] for p in pessoas])]["id"] if pessoas else "",
            "conta_financeira_id": contas[_indice_contendo(info.get("conta_financeira_nome_sugerida") or "", [c["nome"] for c in contas])]["id"] if contas else "",
            "orientacao": "",
        }
        enviado_desta = enviado if tabela_enviada == chave else None
        if enviado_desta is not None:
            for campo in valores:
                valores[campo] = enviado_desta.get(campo, "")
        tabelas.append({
            "chave": chave,
            "titulo": titulo,
            "lancada": False,
            "ignorada": bool(resultado.get("ignorado_propositalmente")),
            "motivo": resultado.get("motivo"),
            "valores": valores,
            "linhas_rateio": _linhas_rateio(_rateio_doc_tabela(info), centros, enviado_desta),
            "fornecedor_sugerido": info.get("fornecedor_nome_sugerido"),
            "centro_por_revisao_manual": info.get("fonte_centro_custo") == "revisao_manual",
        })

    primeira = next(iter(dados.values()), {}) if dados else {}
    return {
        "tabelas": tabelas,
        "competencia": (primeira.get("dados_para_edicao") or {}).get("competencia"),
        "categorias": categorias,
        "pessoas": pessoas,
        "centros_custo": centros,
        "contas_financeiras": contas,
    }


def aprovar_folha_tabela(item_id: str, chave_tabela: str, form: dict, aprovado_por: str, cadastros: dict,
                         forcar_duplicata: bool = False) -> dict:
    return _com_trava(item_id, _aprovar_folha_travado, chave_tabela, form, aprovado_por, cadastros, forcar_duplicata)


def _aprovar_folha_travado(item, chave_tabela, form, aprovado_por, cadastros, forcar_duplicata) -> dict:
    caminho_json, doc = item["caminho_json"], item["caminho_documento"]
    dados = item["info"]["dados"]
    if tipo_pendencia(dados) != "folha":
        return {"status": "erro", "mensagem": "Este item não é de folha de pagamento."}
    resultado = dados.get(chave_tabela)
    if not isinstance(resultado, dict) or resultado.get("sucesso"):
        return {"status": "ja_tratado"}
    info = resultado.get("dados_para_edicao") or {}

    categoria = _por_id(cadastros["categorias"], form.get("categoria_id", ""))
    contato = _por_id(cadastros["pessoas"], form.get("contato_id", ""))
    conta_fin = _por_id(cadastros["contas_financeiras"], form.get("conta_financeira_id", ""))
    if categoria is None or contato is None:
        return {"status": "erro", "mensagem": "Escolha a categoria e o contato."}
    try:
        valor = parse_valor(form.get("valor"))
        data_venc_iso = converter_data(form.get("data_vencimento") or "")
        data_comp_iso = _data_opcional(form.get("data_competencia"))
        rateio = _rateio_das_linhas(len(_rateio_doc_tabela(info)), form, cadastros["centros_custo"])
    except ValueError as e:
        return {"status": "erro", "mensagem": str(e) or "Valor inválido."}

    descricao = (form.get("descricao") or "").strip() or info.get("descricao_sugerida") or ""
    dados_lancamento = {
        "tipo_documento": f"folha_{info.get('tipo_lancamento')}",
        "valor_total": valor,
        "numero_documento": f"{(info.get('tipo_lancamento') or '').upper()}-{info.get('arquivo_bancario') or 'PADRAO'}",
        "descricao": descricao,
        # Sempre vazio: a orientação do revisor nunca vai para a Conta Azul.
        "observacoes": "",
    }
    try:
        token = obter_access_token()
        if not forcar_duplicata:
            duplicata = _checar_duplicata(token, valor, data_venc_iso, categoria, contato)
            if duplicata:
                return {"status": "duplicata", "duplicata": duplicata, "tabela": chave_tabela}
        status, resposta = criar_lancamento(
            token, dados_lancamento, categoria["id"], contato["id"], data_venc_iso,
            rateio_centro_custo=rateio, data_competencia=data_comp_iso or data_venc_iso,
            forma_pagamento=info.get("forma_pagamento_sugerida"),
            conta_financeira_id=conta_fin["id"] if conta_fin else None,
        )
    except ContaAzulAPIError as e:
        return {"status": "erro", "mensagem": f"Erro de comunicação com a Conta Azul: {e}"}
    if status != 200:
        logger.error("Erro %s ao lançar tabela %s via painel: %s", status, chave_tabela, resposta)
        return {"status": "erro", "mensagem": "Erro ao lançar. Veja os logs para detalhes."}

    logger.info("Tabela %s de %s lançada por %s (protocolo %s).", chave_tabela, os.path.basename(caminho_json),
                aprovado_por, resposta.get("protocolo"))
    resultado["sucesso"] = True
    resultado["motivo"] = "Lançado manualmente via painel" + (" (duplicata confirmada pelo usuário)" if forcar_duplicata else "")
    resultado["detalhes"] = {
        "protocolo": resposta.get("protocolo"),
        "tipo": info.get("tipo_lancamento"),
        "descricao": descricao,
        "valor": valor,
        "data_vencimento": data_venc_iso,
        "data_competencia": data_comp_iso or data_venc_iso,
        "categoria": {"id": categoria["id"], "nome": categoria["nome"]},
        "contato": {"id": contato["id"], "nome": contato["nome"]},
        "rateio_aplicado": rateio or [],
        "resposta_criacao": resposta,
        "aprovado_por": aprovado_por,
    }

    # Aprendizado do centro de custo do funcionário: só quando nem documento
    # nem histórico tinham o centro (revisão manual) e a escolha é inequívoca.
    nome_funcionario = info.get("fornecedor_nome_sugerido") or contato["nome"]
    if info.get("fonte_centro_custo") == "revisao_manual" and nome_funcionario and rateio and len(rateio) == 1:
        historico_centros_funcionarios.registrar_centro_custo_funcionario(
            nome_funcionario, rateio[0]["centro_custo_id"], rateio[0]["centro_custo_nome"],
        )
    # Maturação com contador: cada aprovação humana soma +1 para o padrão do
    # documento (libera a autonomia depois de algumas) e grava a orientação.
    orientacao = (form.get("orientacao") or "").strip()
    agente_folha.registrar_aprovacao_manual_padrao(info.get("tipo_lancamento"), orientacao)
    historico_centros_funcionarios.registrar_diretriz_livre("folha", orientacao)

    relevantes = {k: r for k, r in dados.items() if r.get("sucesso") or not r.get("ignorado_propositalmente")}
    concluido = bool(relevantes) and all(r.get("sucesso") for r in relevantes.values())
    if concluido:
        _promover_para_processados(caminho_json, doc, dados)
    else:
        item["info"]["dados"] = dados
        item["info"]["sucesso"] = False
        with open(caminho_json, "w", encoding="utf-8") as f:
            json.dump(item["info"], f, indent=2, ensure_ascii=False)
    return {"status": "lancado", "protocolo": resposta.get("protocolo"), "documento_concluido": concluido, "reextraidos": 0}


# ---------------------------------------------------------------------------
# Duplicata de arquivo (Camada 1): só descartar
# ---------------------------------------------------------------------------
def resumo_duplicado(dados: dict) -> dict:
    anterior = dados.get("registro_anterior") or {}
    return {
        "nome_arquivo": anterior.get("nome_arquivo_original"),
        "processado_em": anterior.get("processado_em"),
        "sucesso": bool(anterior.get("sucesso")),
        "protocolo": anterior.get("protocolo"),
        "motivo": anterior.get("motivo"),
    }


# ---------------------------------------------------------------------------
# Reextração com orientação livre nova
# ---------------------------------------------------------------------------
def reextrair_pendentes_com_nova_diretriz(tipo_documento: str, caminho_json_ignorar: str) -> int:
    """Re-extrai (novo Gemini) os documentos do mesmo tipo que já aguardam
    revisão, para uma orientação livre recém-salva valer também para eles -
    sem isso ela só valeria a partir do próximo documento a chegar.

    Só os campos BRUTOS do documento; nunca re-roda a resolução de
    categoria/contato/banco/rateio (efeito colateral real na Conta Azul).
    Só "nfse" e "comum" - a folha tem estrutura por tabela. Falha num
    documento só é logada. Devolve quantos foram atualizados."""
    if tipo_documento not in ("nfse", "comum"):
        return 0

    atualizados = 0
    for item in listar_pendencias():
        caminho_json = item["caminho_json"]
        if caminho_json == caminho_json_ignorar:
            continue
        dados = item["info"].get("dados")
        doc = item["caminho_documento"]
        if not doc or not isinstance(dados, dict) or tipo_pendencia(dados) != tipo_documento:
            continue

        try:
            if tipo_documento == "nfse":
                dados_novos = agente_nfs_e.extrair_dados_nfse(doc)
                dados_atualizados = {**dados, **dados_novos}
                dados_atualizados["descricao"] = regras_nfse.montar_descricao_nfse(
                    dados_atualizados.get("numero_nota"), dados_atualizados.get("tipo_servico"),
                    dados_atualizados.get("discriminacao_servicos"), dados_atualizados.get("boleto_data_vencimento"),
                )
                dados_atualizados["observacoes"] = regras_nfse.montar_observacao_nfse(
                    dados_atualizados.get("discriminacao_servicos"), dados_atualizados.get("codigo_servico"),
                    dados_atualizados.get("descricao_codigo_servico"),
                )
            else:
                dados_novos = agente_integrado.extrair_dados_documento(doc)
                dados_atualizados = {**dados, **dados_novos}
        except Exception:
            logger.exception("Falha ao reextrair '%s' com a nova orientação livre - mantendo os dados extraídos antes.", doc)
            continue

        item["info"]["dados"] = dados_atualizados
        with open(caminho_json, "w", encoding="utf-8") as f:
            json.dump(item["info"], f, indent=2, ensure_ascii=False)
        atualizados += 1
        logger.info("Documento '%s' reextraído com a nova orientação livre de '%s'.", doc, tipo_documento)
    return atualizados
