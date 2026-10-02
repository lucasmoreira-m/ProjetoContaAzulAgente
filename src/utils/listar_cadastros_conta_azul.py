"""
Ferramenta AUXILIAR, de uso único, para listar centros de custo e contas
financeiras de uma conta Conta Azul - com o único objetivo de descobrir os
UUIDs reais que precisam substituir os hardcoded em regras_rateio.py
(centro_custo_id) e config.py (CONTA_FINANCEIRA_ID_PADRAO) antes de rodar
o projeto contra uma conta diferente da atual (ex: migrar de teste para a
conta real da empresa).

NÃO faz parte do fluxo normal do agente. NÃO lê nem escreve o .env
principal do projeto. NÃO cria, edita nem apaga nada na Conta Azul - só
GET (leitura).

COMO USAR
1. Tenha em mãos: client_id, client_secret e um refresh_token válido DA
   CONTA QUE VOCÊ QUER CONSULTAR (ex: o gerado pelo trocar_code_por_token.py
   para a conta real).
2. Rode: python listar_cadastros_conta_azul.py
3. Digite os 3 valores quando pedido (secretos via getpass, não aparecem
   na tela).
4. O script mostra a lista de centros de custo e contas financeiras dessa
   conta, com nome e id lado a lado - copie o id do que precisar.

ATENÇÃO - o refresh_token é de uso único (mesma regra de sempre): esta
consulta RENOVA o token ao rodar, gerando um novo refresh_token no
processo. O script mostra o novo valor no final - se você pretende usar
essa conta depois num .env de verdade, atualize com o valor novo impresso
aqui, não com o que você digitou no início (que já foi consumido).
"""

from __future__ import annotations

import base64
import getpass
import sys

import requests

URL_TOKEN = "https://api-v2.contaazul.com/oauth/token"
URL_BASE = "https://api-v2.contaazul.com"
TIMEOUT_SEGUNDOS = 20


def obter_access_token_avulso(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Troca o refresh_token por um access_token, sem depender de config.py
    nem do .env principal. Retorna (access_token, novo_refresh_token)."""
    credenciais_base64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credenciais_base64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    resp = requests.post(URL_TOKEN, headers=headers, data=data, timeout=TIMEOUT_SEGUNDOS)
    if resp.status_code != 200:
        print(f"\nErro {resp.status_code} ao renovar token:")
        print(resp.text[:2000])
        sys.exit(1)

    dados = resp.json()
    return dados["access_token"], dados["refresh_token"]


def listar_todas_paginas(access_token: str, caminho: str, chave_itens: str, tamanho_pagina: int = 200) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    todos_itens: list[dict] = []
    pagina = 1

    while True:
        resp = requests.get(
            f"{URL_BASE}{caminho}",
            headers=headers,
            params={"pagina": pagina, "tamanho_pagina": tamanho_pagina},
            timeout=TIMEOUT_SEGUNDOS,
        )
        if resp.status_code != 200:
            print(f"\nErro {resp.status_code} ao consultar {caminho}:")
            print(resp.text[:2000])
            sys.exit(1)

        dados = resp.json()
        itens_pagina = dados.get(chave_itens) or dados.get("items") or dados.get("itens") or []
        todos_itens.extend(itens_pagina)

        if len(itens_pagina) < tamanho_pagina:
            break
        pagina += 1

    return todos_itens


def main() -> None:
    print("=" * 70)
    print("Listagem de centros de custo e contas financeiras - Conta Azul")
    print("=" * 70)
    print(
        "\nEste script NÃO mexe no .env do projeto nem em nenhum arquivo de\n"
        "código - só consulta (GET) e imprime na tela. Você copia os IDs\n"
        "que precisar manualmente para regras_rateio.py / config.py.\n"
    )

    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client Secret (não aparece na tela): ").strip()
    refresh_token = getpass.getpass("Refresh Token da conta a consultar (não aparece na tela): ").strip()

    if not client_id or not client_secret or not refresh_token:
        print("\nErro: os três campos são obrigatórios. Abortando.")
        sys.exit(1)

    print("\nAutenticando...")
    access_token, novo_refresh_token = obter_access_token_avulso(client_id, client_secret, refresh_token)

    print("\n" + "=" * 70)
    print("CENTROS DE CUSTO")
    print("=" * 70)
    centros = listar_todas_paginas(access_token, "/v1/centro-de-custo", "itens")
    for c in sorted(centros, key=lambda x: x.get("nome", "")):
        codigo = c.get("codigo") or ""
        print(f"  {c.get('id')}  |  {codigo} - {c.get('nome', '')}")
    print(f"\nTotal: {len(centros)} centro(s) de custo.")

    print("\n" + "=" * 70)
    print("CONTAS FINANCEIRAS")
    print("=" * 70)
    contas = listar_todas_paginas(access_token, "/v1/conta-financeira", "itens")
    for c in sorted(contas, key=lambda x: x.get("nome", "")):
        print(f"  {c.get('id')}  |  {c.get('nome', '')}")
    print(f"\nTotal: {len(contas)} conta(s) financeira(s).")

    print("\n" + "=" * 70)
    print("IMPORTANTE")
    print("=" * 70)
    print(
        f"O refresh_token que você digitou foi CONSUMIDO nesta consulta.\n"
        f"Se for usar esta conta depois, o refresh_token válido agora é:\n\n"
        f"{novo_refresh_token}\n\n"
        f"Atualize o .env (ou o arquivo onde você guarda essa credencial) com\n"
        f"este valor novo, não com o que você digitou no início."
    )


if __name__ == "__main__":
    main()
