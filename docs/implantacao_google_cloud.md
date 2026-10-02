# Implantação no Google Cloud Run — roteiro manual pelo console

Painel FastAPI do Agente Conta Azul no **Cloud Run**, região **São Paulo
(`southamerica-east1`)**, com:
- **login Microsoft 365** dentro do painel (só contas da empresa);
- configuração e segredos num **`secrets.toml`** guardado no Secret Manager e
  entregue ao painel como arquivo. Lá **não existe `.env`**;
- o **refresh token** da Conta Azul num segredo separado, porque ele muda a cada
  renovação e o painel precisa gravar o novo;
- dados num **bucket do Cloud Storage** montado em `/dados`: PDFs, revisão,
  histórico e o aprendizado do agente.

Imagem: `conta-azul-agente-v3.tar` (está em Downloads; não tem nenhum segredo).

Troque nos passos abaixo:
- `ID-DO-PROJETO`: o ID do projeto no Google Cloud;
- `NOME-DO-BUCKET`: o nome escolhido no passo 4.

---

## 1. Projeto e faturamento
1. Acesse https://console.cloud.google.com e escolha (ou crie) o projeto no seletor do topo.
2. **Faturamento**: confirme que o projeto tem uma conta de faturamento vinculada.

## 2. Ativar as APIs
Menu **APIs e serviços → Biblioteca**. Procure e clique em **Ativar** em cada uma:
- Cloud Run Admin API
- Artifact Registry API
- Secret Manager API

## 3. Repositório da imagem e envio
1. **Artifact Registry → Repositórios → Criar repositório**
   - Nome: `agente`
   - Formato: **Docker**
   - Tipo de local: **Região** → `southamerica-east1 (São Paulo)`
   - Clique em **Criar**.
2. Abra o **Cloud Shell**, pelo ícone `>_` no topo do console. Ele já vem logado e com Docker.
3. No Cloud Shell, use **⋮ → Fazer upload** e envie o `conta-azul-agente-v3.tar`.
4. Cole os comandos, trocando `ID-DO-PROJETO`:
   ```bash
   docker load -i conta-azul-agente-v3.tar
   docker tag seuregistro.azurecr.io/conta-azul-agente:v3 southamerica-east1-docker.pkg.dev/ID-DO-PROJETO/agente/conta-azul-agente:v3
   gcloud auth configure-docker southamerica-east1-docker.pkg.dev --quiet
   docker push southamerica-east1-docker.pkg.dev/ID-DO-PROJETO/agente/conta-azul-agente:v3
   rm conta-azul-agente-v3.tar
   ```
5. Confira em **Artifact Registry → agente** se aparece `conta-azul-agente` com a tag `v3`.

## 4. Bucket dos dados
1. **Cloud Storage → Buckets → Criar**
   - Nome: único no mundo todo, ex.: `empresa-agente-conta-azul-dados`
   - Local: **Região** → `southamerica-east1`
   - Classe: **Standard**
   - Controle de acesso: **Uniforme**, com **"Impor a prevenção do acesso público"** marcado
   - Proteção: pode deixar o padrão. Ativar o **controle de versão de objetos** é
     recomendado: ele guarda as versões anteriores dos registros, como backup extra.
2. Entre no bucket e use **Fazer upload de arquivos** para enviar, na **raiz**, os
   registros de aprendizado do PC que está em produção hoje:
   - `fornecedores_registry.json`
   - `historico_centros_funcionarios.json`
   - `arquivos_processados_registry.json`

   Sem eles, o agente começa "sem memória". As pastas `entrada/`, `revisao/` etc.
   são criadas pelo próprio painel.

## 5. Segredos (Secret Manager)
No Google ficam **só dois segredos**. **Segurança → Secret Manager → Criar
secret**, uma vez para cada:

### 5.1 `secrets-toml`: toda a configuração
1. No seu PC, faça uma **cópia** do `secrets.toml` do projeto e preencha a cópia:
   ```toml
   [conta_azul]
   CONTA_AZUL_AMBIENTE = "producao"        # ou "teste"
   CONTA_AZUL_CLIENT_ID = "..."
   CONTA_AZUL_CLIENT_SECRET = "..."
   CONTA_AZUL_REFRESH_TOKEN = ""           # DEIXE VAZIO - vai no segredo 5.2

   [gemini]
   GEMINI_API_KEY = "..."

   [microsoft]
   MS_CLIENT_ID = "..."                    # ID do aplicativo (cliente)
   MS_TENANT_ID = "..."                    # ID do diretório (locatário)
   MS_CLIENT_SECRET = "..."                # o VALOR do segredo
   MS_REDIRECT_URI = ""                    # preencher no passo 8
   USUARIOS_PERMITIDOS = ""                # opcional
   SESSAO_CHAVE = "..."                    # texto longo e aleatório (ex.: 50 letras e números)

   [google]
   GOOGLE_SECRET_MANAGER_PROJETO = "ID-DO-PROJETO"
   ```
2. Crie o secret **`secrets-toml`** e, em **Valor do secret**, use **Fazer upload
   de arquivo** e escolha essa cópia. Também dá para colar o conteúdo.
3. **Apague a cópia** do seu PC depois do upload.

### 5.2 `conta-azul-refresh-token`: só o token
- Nome: **`conta-azul-refresh-token`**, exatamente assim.
- Valor: um refresh token **só para o Google**.

> ⚠️ **Refresh token exclusivo.** Ele é de uso único e muda a cada renovação. Se o
> mesmo token for usado também num PC, a primeira renovação de um derruba o
> outro. Gere um token novo para o Google (`src/utils/trocar_code_por_token.py`)
> e não coloque esse token em nenhum `.env` nem `secrets.toml`.

## 6. Conta de serviço e permissões
1. **IAM e administrador → Contas de serviço → Criar conta de serviço**
   - Nome: `agente-conta-azul` → **Criar e continuar** → **Concluir**, sem papéis aqui.
2. **IAM e administrador → IAM → Conceder acesso**
   - Novos principais: o e-mail da conta criada (`agente-conta-azul@ID-DO-PROJETO.iam.gserviceaccount.com`)
   - Papéis:
     - **Acessador de secrets do Secret Manager**, para ler os segredos;
     - **Administrador de versões de secrets do Secret Manager**, para gravar o
       refresh token renovado e apagar as versões antigas.
   - **Salvar**
3. **Cloud Storage → o bucket → Permissões → Conceder acesso**
   - Principal: o mesmo e-mail
   - Papel: **Usuário de objetos do Storage**
   - **Salvar**

## 7. Serviço no Cloud Run
**Cloud Run → Implantar contêiner → Serviço**
- **URL da imagem**: selecione `agente/conta-azul-agente:v3`
- **Nome do serviço**: `conta-azul-agente`
- **Região**: `southamerica-east1 (São Paulo)`
- **Autenticação**: **Permitir acesso público**. Quem protege é o login Microsoft do
  painel: sem login, nenhuma página abre.
- **Faturamento**: **Com base em instâncias** (CPU sempre alocada). O
  "Processar entrada" roda em segundo plano, e no outro modo o Google pausa a
  CPU entre as requisições.
- **Escalonamento**: mínimo **0**, máximo **1** instância. **O máximo precisa ser 1**:
  o refresh token e as travas não suportam duas cópias rodando juntas.
- **Entrada**: Todos

Abra **Contêineres, volumes, rede, segurança**:
- Aba **Volumes → Adicionar volume**
  - Tipo: **Bucket do Cloud Storage** → escolha o bucket do passo 4
  - Nome do volume: `dados` → **Concluído**
- **Adicionar volume** de novo:
  - Tipo: **Secret** → secret `secrets-toml`, versão **latest**
  - Caminho: `secrets.toml`
  - Nome do volume: `segredos` → **Concluído**
- Aba **Contêiner(es)**:
  - **Porta do contêiner**: `8000`
  - **Recursos**: Memória **1 GiB**, CPU **1**
  - **Montagens de volume → Montar volume**, duas vezes:
    - volume `dados`, caminho **`/dados`**
    - volume `segredos`, caminho **`/segredos`**
  - **Variáveis e secrets → Adicionar variável**. Só uma, porque o resto está no `secrets.toml`:

    | Nome | Valor |
    |---|---|
    | `SECRETS_TOML_PATH` | `/segredos/secrets.toml` |
- Aba **Segurança**: **Conta de serviço** → `agente-conta-azul`
- **Ambiente de execução**: **Segunda geração**, que é obrigatória para montar o bucket.

Clique em **Criar**. Ao final aparece a **URL** do serviço, algo como
`https://conta-azul-agente-xxxxxxxx.southamerica-east1.run.app`.

## 8. Endereço de retorno do login
1. **Secret Manager → `secrets-toml` → Nova versão**: envie o mesmo arquivo do
   passo 5.1, agora com `MS_REDIRECT_URI = "URL-DO-SERVIÇO/auth/callback"`.
2. **Cloud Run → conta-azul-agente → Editar e implantar nova revisão → Implantar**,
   sem mudar nada. É isso que faz o painel ler a versão nova do `secrets.toml`.
3. No portal do Azure, em **Entra ID → Registros de aplicativo → o app →
   Autenticação → Web → Adicionar URI**, cadastre o **mesmo** endereço
   (`URL-DO-SERVIÇO/auth/callback`) e **salve**. Se você não tiver acesso, peça ao TI.

## 9. Testar
1. `URL-DO-SERVIÇO/saude` deve responder `{"ok": true, "ambiente": "producao"}`.
2. Abra a URL: deve aparecer a tela **Entrar com Microsoft**. Entre com a conta da
   Empresa: o seu e-mail aparece no topo.
3. Envie um PDF de valor baixo pela aba Entrada, clique em **Processar entrada**
   e acompanhe.
4. Confira no bucket se apareceram as pastas `entrada/`, `revisao/` e `processados/`.

## Trocar uma credencial depois
- **Qualquer item do `secrets.toml`** (Conta Azul, Gemini, login…): Secret Manager →
  `secrets-toml` → **Nova versão** com o arquivo atualizado → Cloud Run → **Editar e
  implantar nova revisão**.
- **Refresh token**: Secret Manager → `conta-azul-refresh-token` → **Nova versão** →
  nova revisão no Cloud Run. Normalmente não é preciso: o painel renova sozinho.

## Se der errado
Veja **Cloud Run → conta-azul-agente → Registros**. Os casos mais comuns:

| Mensagem no registro | Causa e correção |
|---|---|
| `Segredo(s) não encontrado(s) no Secret Manager` | Falta o `conta-azul-refresh-token` (passo 5.2) ou algum campo do `secrets.toml` está vazio. |
| `Configuração incompleta` | O `secrets.toml` não foi encontrado: confira o volume `segredos` em `/segredos` e a variável `SECRETS_TOML_PATH` (passo 7). |
| `403` / `Permission denied` no Secret Manager | Falta papel da conta de serviço (passo 6.2). |
| `Permission denied` em `/dados` | Falta o papel no bucket (passo 6.3). Se o papel estiver certo, a montagem precisa da opção `uid=1000,gid=1000`. |
| `AADSTS50011` na tela da Microsoft | O endereço de retorno não confere (passo 8): os dois lados precisam ser idênticos. |
| `FALHA ao gravar o refresh_token renovado` | Falta o papel "Administrador de versões" (passo 6.2). Corrija **antes** de o serviço reiniciar. |
| Erro ao "Permitir acesso público" | Política da organização bloqueando acesso público: o TI precisa liberar para este serviço. |

## Custo estimado
Com mínimo 0 instâncias, só cobra enquanto alguém está usando. O bucket e o Secret
Manager custam centavos por mês: o painel apaga sozinho as versões antigas do
refresh token. O Cloud Run fica em poucos dólares por mês no volume de uso da empresa.
