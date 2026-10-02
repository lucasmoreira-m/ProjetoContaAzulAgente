"""
Ferramenta AUXILIAR, de uso único, para trocar um `authorization_code` do
fluxo OAuth2 da Conta Azul por um `refresh_token` novo - útil quando você
está autorizando o app contra uma conta DIFERENTE da que já está em uso
no .env principal (ex: gerando as credenciais da conta real da empresa, para
guardar prontas, sem ainda conectar a nenhum processo em execução).

NÃO faz parte do fluxo normal do agente (monitor_pasta.py, painel_revisao.py
nunca chamam este script). É uma ferramenta manual, para rodar você mesmo,
uma vez, sempre que precisar autorizar uma conta nova.

COMO USAR
1. Pegue a URL de autorização exata do seu app no Portal do Desenvolvedor
   da Conta Azul ("Minhas Aplicações" > seu app).
2. Abra essa URL num navegador DESLOGADO da conta que você não quer usar
   agora (ou aba anônima), faça login na conta que você QUER autorizar, e
   aceite a autorização.
3. Você será redirecionado para https://contaazul.com/?code=ALGUMA_COISA
   - copie o valor de "code" da barra de endereço.
4. Rode este script:
       python trocar_code_por_token.py
   Ele vai pedir o `code`, o `client_id` e o `client_secret` de forma
   interativa (não hardcoded, para não deixar segredo salvo no arquivo).
5. O script imprime o refresh_token gerado E grava num arquivo separado
   (nunca no .env em uso) para você copiar com calma.

SEGURANÇA
- Este script NUNCA lê nem escreve o .env principal do projeto - evita
  qualquer risco de misturar credenciais de contas diferentes por engano.
- O client_secret é pedido via getpass (não aparece na tela, não fica no
  histórico do terminal).
- O refresh_token gerado é salvo em um arquivo com nome que você escolhe,
  para você decidir onde/como guardar (ex: .env.producao) - o script não
  decide isso por você.
"""

from __future__ import annotations

import base64
import getpass
import sys
from datetime import datetime

import requests

URL_TOKEN = "https://api-v2.contaazul.com/oauth/token"
REDIRECT_URI_PADRAO = "https://contaazul.com/"


def _pedir_secreto(prompt: str) -> str:
    """Tenta pedir o valor de forma oculta (getpass). Alguns terminais do
    Windows (dependendo de como foram abertos) disparam KeyboardInterrupt
    sozinhos ao tentar ocultar a digitação - nesse caso, cai para
    digitação visível, avisando antes, em vez de travar o script."""
    try:
        return getpass.getpass(prompt).strip()
    except KeyboardInterrupt:
        print(
            "\n(O modo oculto de digitação não funcionou neste terminal - "
            "vai aparecer o texto digitado na tela a partir de agora.)"
        )
        return input(prompt).strip()


def main() -> None:
    print("=" * 70)
    print("Troca de authorization_code por refresh_token - Conta Azul")
    print("=" * 70)
    print(
        "\nEste script NÃO mexe no .env do projeto. Ele só troca um código\n"
        "por um token e mostra/salva o resultado para você copiar manualmente\n"
        "para onde quiser (ex: um .env.producao separado).\n"
    )

    client_id = input("Client ID (o mesmo já usado no app): ").strip()
    client_secret = _pedir_secreto("Client Secret (não aparece na tela): ")
    code = input("Authorization code (copiado da URL de retorno): ").strip()

    redirect_uri = input(
        f"Redirect URI [Enter para usar o padrão: {REDIRECT_URI_PADRAO}]: "
    ).strip() or REDIRECT_URI_PADRAO

    if not client_id or not client_secret or not code:
        print("\nErro: client_id, client_secret e code são todos obrigatórios. Abortando.")
        sys.exit(1)

    credenciais_base64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credenciais_base64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    print("\nTrocando o code por tokens...")
    try:
        resp = requests.post(URL_TOKEN, headers=headers, data=data, timeout=20)
    except requests.RequestException as e:
        print(f"\nFalha de rede ao chamar a Conta Azul: {e}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"\nErro {resp.status_code} da Conta Azul:")
        print(resp.text[:2000])
        print(
            "\nCausas comuns: code já usado (cada code só serve UMA vez e "
            "expira em poucos minutos - gere um novo se precisar tentar de "
            "novo), redirect_uri diferente do configurado no app, ou "
            "client_id/client_secret incorretos."
        )
        sys.exit(1)

    dados = resp.json()
    access_token = dados.get("access_token", "")
    refresh_token = dados.get("refresh_token", "")

    print("\n" + "=" * 70)
    print("SUCESSO")
    print("=" * 70)
    print(f"access_token  (expira em {dados.get('expires_in', '?')}s, não precisa guardar): {access_token[:20]}...")
    print(f"refresh_token (GUARDE ESTE):\n{refresh_token}")

    nome_arquivo = input(
        "\nNome do arquivo para salvar este resultado "
        "[Enter para 'refresh_token_novo.txt']: "
    ).strip() or "refresh_token_novo.txt"

    with open(nome_arquivo, "w", encoding="utf-8") as f:
        f.write(f"# Gerado em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# client_id usado: {client_id}\n")
        f.write(f"# redirect_uri usada: {redirect_uri}\n\n")
        f.write(f"CONTA_AZUL_CLIENT_ID={client_id}\n")
        f.write(f"CONTA_AZUL_CLIENT_SECRET={client_secret}\n")
        f.write(f"CONTA_AZUL_REFRESH_TOKEN={refresh_token}\n")

    print(f"\nSalvo em: {nome_arquivo}")
    print(
        "\nPRÓXIMOS PASSOS (manuais, este script não faz por você):\n"
        f"1. Renomeie/copie o conteúdo de '{nome_arquivo}' para o .env que\n"
        "   você quer usar para esta conta (ex: .env.producao) - lembre de\n"
        "   completar também GEMINI_API_KEY e as demais variáveis que o\n"
        "   config.py exige.\n"
        f"2. Apague o arquivo '{nome_arquivo}' depois de copiar o conteúdo -\n"
        "   ele tem um segredo em texto puro, não precisa ficar por aí.\n"
        "3. NÃO aponte nenhum processo (monitor_pasta.py, painel_revisao.py)\n"
        "   para esse novo .env até estar pronto para usar essa conta de\n"
        "   verdade - até lá, é só uma credencial guardada.\n"
    )


if __name__ == "__main__":
    main()
