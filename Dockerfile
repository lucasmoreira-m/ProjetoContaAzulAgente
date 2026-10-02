# Imagem do painel web (FastAPI) para o Azure Container Apps ou o Google
# Cloud Run (2026-10-01).
#
# Gerar e enviar (na raiz do projeto):
#   docker build -t seuregistro.azurecr.io/conta-azul-agente:v3 .
#   az acr login --name seuregistro
#   docker push seuregistro.azurecr.io/conta-azul-agente:v3
# ou, sem Docker local:
#   az acr build --registry seuregistro --image conta-azul-agente:v3 .
#
# Nada de credencial na imagem: os segredos vêm do Key Vault do Azure
# (AZURE_KEY_VAULT_URL) ou do Secret Manager do Google
# (GOOGLE_SECRET_MANAGER_PROJETO), e só o código de src/ é copiado. Dados
# (entrada/, revisao/, processados/, registros de aprendizado, backups) ficam
# em /dados, que precisa ser um volume (Azure
# Files ou bucket do Cloud Storage) - o disco do container é apagado a cada
# reinício. Roteiros no README: "Hospedar no Azure" e "Hospedar no Google".
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    # Exige o login Microsoft 365 (Easy Auth) em toda página.
    EXIGIR_LOGIN_M365=1 \
    # Cache do access_token fora do volume: é descartável e o SMB do Azure
    # Files não precisa disso.
    CACHE_ACCESS_TOKEN_PATH=/tmp/.access_token_cache.json \
    CACHE_ACCESS_TOKEN_LOCK_PATH=/tmp/.access_token_cache.lock \
    # Travas de arquivo no disco local, fora do volume: o bucket do Google
    # (Cloud Storage) não tem trava de arquivo; com 1 instância, basta.
    PASTA_TRAVAS=/tmp/travas

WORKDIR /app
COPY requirements.txt requirements-azure.txt ./
RUN pip install --no-cache-dir -r requirements-azure.txt

COPY src ./src

# Usuário sem privilégio. /dados é o ponto de montagem do Azure Files; as
# pastas de trabalho são criadas pelo próprio painel ao subir.
RUN useradd --create-home --uid 1000 agente \
    && mkdir -p /dados \
    && chown agente:agente /dados
USER agente

# Todos os caminhos do config.py são relativos à pasta de trabalho.
WORKDIR /dados
EXPOSE 8000

# Um processo só (workers=1): o painel guarda o andamento do lote em memória
# e o Container App deve rodar com 1 réplica (refresh_token de uso único).
CMD ["python", "-m", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
