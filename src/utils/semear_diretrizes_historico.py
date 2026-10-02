"""
Ferramenta manual, repetível - semeia as Diretrizes Globais de Texto (ver
historico_centros_funcionarios.py: buscar_diretrizes_globais_texto/
registrar_diretriz_global_texto) a partir do histórico REAL de lançamentos
já feitos na Conta Azul, em vez de esperar aprovações manuais repetidas no
painel para cada palavra-chave nova.

POR QUE ISSO EXISTE:
As Diretrizes Globais de Texto (agente_integrado.py + painel_revisao.py)
começam vazias e só crescem por aprovação humana - correto para nunca
"inventar" uma regra que ninguém confirmou, mas significa esperar a MESMA
palavra-chave aparecer e ser aprovada várias vezes antes do sistema confiar
nela. Como a empresa já tem meses/anos de lançamentos reais na Conta Azul
com padrões claros (ex: "faxina" quase sempre na mesma categoria, em
vários fornecedores/endereços diferentes), dá para adiantar esse
aprendizado minerando esse histórico - com critérios conservadores para
não gravar ruído.

CRITÉRIOS (todos precisam bater para uma palavra virar candidata):
1. Só palavras PURAMENTE ALFABÉTICAS, >= 4 letras (o regex já descarta
   números, códigos de NF, datas, conectores curtos como "de"/"da"/"do").
2. Aparece em pelo menos MIN_OCORRENCIAS lançamentos distintos.
3. Aparece em lançamentos de pelo menos MIN_FORNECEDORES_DISTINTOS
   fornecedores DIFERENTES - descarta palavras que na verdade são um
   artefato de endereço/nome de UM fornecedor só (esse caso já é coberto
   por fornecedores_registry.py via CNPJ, não precisa duplicar aqui).
4. Pelo menos MIN_CONFIANCA (80% por padrão) das vezes que a palavra
   aparece, o lançamento usa a MESMA categoria - palavras "ambíguas" (que
   aparecem espalhadas por categorias diferentes) são descartadas, porque
   não são um sinal confiável.
5. Não pode ser uma palavra que também aparece no NOME de algum fornecedor
   do lote (ver _nomes_conhecidos) - descarta nome de pessoa/empresa que
   coincidentemente bateu nos critérios acima.

Toda diretriz gravada por aqui entra com origem="inferido_do_historico"
(distinta de "aprovado_humano") - NUNCA lançada de verdade, e nunca
sobrescreve uma diretriz que já foi confirmada por um humano de verdade
(ver historico_centros_funcionarios.registrar_diretriz_global_texto).

COMO USAR (rodar da raiz do projeto):
    python src/utils/semear_diretrizes_historico.py                 # dry-run - só mostra os candidatos, não grava nada
    python src/utils/semear_diretrizes_historico.py --aplicar        # grava de verdade em historico_centros_funcionarios.json
    python src/utils/semear_diretrizes_historico.py --dias 180       # janela menor (padrão: 365 dias)
    python src/utils/semear_diretrizes_historico.py --min-ocorrencias 5 --min-fornecedores 3 --min-confianca 0.9  # critérios mais estritos
"""

from __future__ import annotations

import argparse
import collections
import re
import unicodedata

from src.core import conta_azul_auth as auth
from src.core import conta_azul_client as ca
from src.utils import historico_centros_funcionarios as hcf

# Conectores/termos genéricos com >= 4 letras que passariam pelo filtro de
# tamanho mas não são sinal nenhum. Os critérios estatísticos (ocorrências/
# fornecedores/confiança) filtram boa parte do ruído sozinhos, mas a
# primeira rodada real deste script (ver histórico de conversa) mostrou uma
# lacuna: texto jurídico/RH BOILERPLATE (rescisão, PPLR etc.) se repete
# quase idêntico entre VÁRIOS funcionários diferentes (cada demissão é um
# "fornecedor" diferente para fins de contagem), então passa até por um
# filtro de diversidade de fornecedores sem ser um "keyword de negócio"
# generalizável de verdade - por isso a lista abaixo inclui esses termos
# encontrados na prática, além dos conectores óbvios.
PALAVRAS_IGNORADAS = {
    "para", "como", "sobre", "esta", "esse", "essa", "pelo", "pela", "desde",
    "quando", "onde", "isso", "aquele", "aquela", "conforme", "referente",
    "ltda", "eireli", "epp", "sociedade", "limitada", "unipessoal",
    # boilerplate jurídico/RH observado na primeira mineração real (rescisão,
    # PPLR, empréstimo, crédito indevido) - descreve a NATUREZA do documento,
    # não um tipo de despesa generalizável:
    "entre", "partes", "prazo", "determinado", "devolvido", "duplicidade",
    "socios", "administrativo", "colaborador", "personalizado", "realizado",
    "mensal", "dois", "concessao", "espaco", "acessorios", "quadros",
    "impressoes", "republoca",
}

MIN_TAMANHO_PALAVRA = 4
_REGEX_PALAVRA = re.compile(r"[a-z]{%d,}" % MIN_TAMANHO_PALAVRA)


def _normalizar(texto: str | None) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("utf-8")
    return texto.lower()


def _extrair_palavras(descricao: str | None) -> set[str]:
    """Extrai palavras candidatas de UMA descrição - set() (não list) de
    propósito: uma palavra repetida na MESMA descrição conta só 1 vez para
    as estatísticas de ocorrência (evita que uma descrição verborrágica
    infle artificialmente a contagem de uma palavra)."""
    texto_norm = _normalizar(descricao)
    return {p for p in _REGEX_PALAVRA.findall(texto_norm) if p not in PALAVRAS_IGNORADAS}


def buscar_historico(dias: int) -> list[dict]:
    """Busca TODOS os lançamentos em contas a pagar dos últimos `dias`
    dias, paginando até a lista vir vazia. Só leitura - nunca cria, edita
    nem apaga nada na Conta Azul.

    Chama `auth.obter_access_token()` A CADA página (não uma vez só no
    início) - é barato (devolve o valor em cache quando ainda válido, só
    faz round-trip de rede quando precisa renovar), e evita usar um token
    ficando obsoleto no meio de uma paginação longa (foi exatamente isso
    que causou um 401 na primeira tentativa real desta ferramenta)."""
    from datetime import date, timedelta

    hoje = date.today()
    data_de = (hoje - timedelta(days=dias)).isoformat()
    data_ate = hoje.isoformat()

    todos: list[dict] = []
    pagina = 1
    while True:
        access_token = auth.obter_access_token()
        resposta = ca.get(
            access_token, "/v1/financeiro/eventos-financeiros/contas-a-pagar/buscar",
            params={
                "data_vencimento_de": data_de, "data_vencimento_ate": data_ate,
                "pagina": pagina, "tamanho_pagina": 200,
            },
        )
        itens = resposta.get("itens") or resposta.get("items") or []
        if not itens:
            break
        todos.extend(itens)
        pagina += 1
        if pagina > 200:
            print(f"AVISO: parando em 200 páginas por segurança - pode haver mais lançamentos além dos {len(todos)} já buscados.")
            break

    return todos


def _nomes_conhecidos(itens: list[dict]) -> set[str]:
    """Reúne todas as palavras que aparecem no NOME do fornecedor de algum
    lançamento do lote - usado para descartar candidatos que são na
    verdade nome de pessoa/empresa (ex: sobrenome de um funcionário
    reembolsado), não uma palavra-chave de negócio generalizável como
    "hotel"/"combustível". Isso resolve o principal problema encontrado na
    primeira mineração real: nomes como "carvalho"/"welberth"/"tanajura"
    passavam pelos filtros estatísticos porque cada um aparecia em vários
    lançamentos processados por fornecedores (agências/operadoras)
    diferentes, mas o sinal real ali é "nome de quem viajou", não um tipo
    de despesa - iria confundir qualquer documento futuro que por
    coincidência mencionasse a mesma palavra por outro motivo."""
    nomes: set[str] = set()
    for item in itens:
        fornecedor_nome = (item.get("fornecedor") or {}).get("nome")
        nomes |= _extrair_palavras(fornecedor_nome)
    return nomes


def minerar_diretrizes(
    itens: list[dict],
    min_ocorrencias: int,
    min_fornecedores: int,
    min_confianca: float,
) -> list[dict]:
    """Aplica os critérios (ver docstring do módulo) e devolve a lista de
    candidatos a Diretriz Global de Texto, ordenada da palavra mais
    frequente para a menos frequente."""
    nomes_conhecidos = _nomes_conhecidos(itens)
    total_por_palavra: collections.Counter = collections.Counter()
    fornecedores_por_palavra: dict[str, set[str]] = collections.defaultdict(set)
    contagem_categoria: collections.Counter = collections.Counter()  # (palavra, categoria) -> contagem
    contagem_centro: collections.Counter = collections.Counter()  # (palavra, centro) -> contagem

    for item in itens:
        palavras = _extrair_palavras(item.get("descricao"))
        if not palavras:
            continue

        categorias_item = item.get("categorias") or []
        categoria_nome = categorias_item[0]["nome"] if categorias_item else None

        # Só usa centro de custo quando o lançamento tem EXATAMENTE um -
        # com rateio por vários centros, não dá pra saber qual centro
        # associar à palavra-chave (mesma cautela do wiring em
        # agente_integrado.py: centro de custo por palavra-chave só é um
        # fallback de UM centro só, nunca substitui um rateio real).
        centros_item = item.get("centros_de_custo") or []
        centro_nome = centros_item[0]["nome"] if len(centros_item) == 1 else None

        fornecedor_nome = (item.get("fornecedor") or {}).get("nome")

        for palavra in palavras:
            total_por_palavra[palavra] += 1
            if fornecedor_nome:
                fornecedores_por_palavra[palavra].add(fornecedor_nome)
            if categoria_nome:
                contagem_categoria[(palavra, categoria_nome)] += 1
            if centro_nome:
                contagem_centro[(palavra, centro_nome)] += 1

    candidatos = []
    for palavra, total in total_por_palavra.items():
        if palavra in nomes_conhecidos:
            continue
        if total < min_ocorrencias:
            continue
        if len(fornecedores_por_palavra[palavra]) < min_fornecedores:
            continue

        categorias_desta_palavra = {
            cat: n for (p, cat), n in contagem_categoria.items() if p == palavra
        }
        if not categorias_desta_palavra:
            continue
        categoria_top, contagem_top = max(categorias_desta_palavra.items(), key=lambda kv: kv[1])
        confianca_categoria = contagem_top / total
        if confianca_categoria < min_confianca:
            continue

        centro_top = None
        centros_desta_palavra = {c: n for (p, c), n in contagem_centro.items() if p == palavra}
        if centros_desta_palavra:
            nome_centro_top, contagem_centro_top = max(centros_desta_palavra.items(), key=lambda kv: kv[1])
            if contagem_centro_top / total >= min_confianca:
                centro_top = nome_centro_top

        candidatos.append({
            "palavra": palavra,
            "categoria_nome": categoria_top,
            "centro_custo_nome": centro_top,
            "ocorrencias": total,
            "confianca_categoria": confianca_categoria,
            "fornecedores_distintos": len(fornecedores_por_palavra[palavra]),
        })

    candidatos.sort(key=lambda c: -c["ocorrencias"])
    return candidatos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aplicar", action="store_true", help="Grava de verdade (padrão: dry-run, só mostra os candidatos).")
    parser.add_argument("--dias", type=int, default=365, help="Janela de dias para trás a consultar (padrão: 365).")
    parser.add_argument("--min-ocorrencias", type=int, default=3, help="Mínimo de lançamentos distintos com a palavra (padrão: 3).")
    parser.add_argument("--min-fornecedores", type=int, default=2, help="Mínimo de fornecedores DIFERENTES com a palavra (padrão: 2).")
    parser.add_argument("--min-confianca", type=float, default=0.8, help="Fração mínima de concentração numa única categoria (padrão: 0.8).")
    args = parser.parse_args()

    print(f"Buscando lançamentos dos últimos {args.dias} dias (só leitura)...")
    itens = buscar_historico(args.dias)
    print(f"{len(itens)} lançamentos encontrados.\n")

    candidatos = minerar_diretrizes(itens, args.min_ocorrencias, args.min_fornecedores, args.min_confianca)

    print(
        f"{len(candidatos)} palavra(s)-chave candidata(s) "
        f"(>= {args.min_ocorrencias} ocorrências, >= {args.min_fornecedores} fornecedores distintos, "
        f">= {args.min_confianca * 100:.0f}% de confiança na categoria):\n"
    )
    for c in candidatos:
        centro_txt = c["centro_custo_nome"] or "-"
        print(
            f"  '{c['palavra']}' -> categoria \"{c['categoria_nome']}\" "
            f"(confiança {c['confianca_categoria'] * 100:.0f}%, {c['ocorrencias']} ocorrências, "
            f"{c['fornecedores_distintos']} fornecedores) | centro de custo: {centro_txt}"
        )

    if not candidatos:
        print("\nNenhum candidato passou os critérios - nada a gravar.")
        return

    if not args.aplicar:
        print(f"\nModo DRY-RUN (padrão) - nada foi gravado. Rode com --aplicar para gravar de verdade.")
        return

    print(f"\nGravando {len(candidatos)} diretriz(es) com origem='inferido_do_historico'...")
    for c in candidatos:
        hcf.registrar_diretriz_global_texto(
            c["palavra"], categoria_nome=c["categoria_nome"], centro_custo_nome=c["centro_custo_nome"],
            origem="inferido_do_historico", incrementar_por=c["ocorrencias"],
        )
    print("Concluído - diretrizes gravadas em historico_centros_funcionarios.json (chave '_diretrizes_globais_texto').")


if __name__ == "__main__":
    main()
