# Imagen de UN microservicio de AegisVolt.
#   docker build -f services/Dockerfile --build-arg SERVICE=billing_wallet -t aegisvolt/billing-wallet-service:TAG .
# Cada imagen contiene SOLO su paquete + la librería común: despliegue independiente.

FROM python:3.13-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY services/requirements.txt /tmp/requirements.txt
RUN pip install --prefix=/install -r /tmp/requirements.txt

FROM python:3.13-slim
ARG SERVICE
ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
    SERVICE_MODULE=${SERVICE} APP_VERSION=${APP_VERSION}
COPY --from=deps /install /usr/local
WORKDIR /app
COPY services/aegis_common ./aegis_common
COPY services/${SERVICE} ./${SERVICE}
RUN useradd --uid 10001 --no-create-home appuser
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE}.app:app --host 0.0.0.0 --port 8000 --no-access-log --proxy-headers"]
