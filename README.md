# ⚡ AegisVolt

Plataforma cloud-native de gestión de carga para flotas eléctricas y electrolineras B2B.
Permite reservar bahías de carga, cobrar a la billetera de la flota, activar la transferencia
de energía y auditar la telemetría. Todo corre sobre **AWS emulado con Floci**, **Kubernetes
(kind)** y **Terraform**, con tres ambientes aislados: **dev**, **test** y **prod**.

> Guía de la defensa oral, con cada pregunta del banco y su demo: [`docs/SUSTENTACION.md`](docs/SUSTENTACION.md)

---

## Arquitectura

```
 Navegador ──HTTP──► Ingress nginx (dev|test|prod.aegisvolt.localhost)
                        │  /          → frontend (React)
                        │  /graphql   → graphql-gateway
 Cliente VPC ──► API Gateway PRIVADO (VPCE execute-api) ──► VPC Link/NLB ──► NodePort 3008X ─┘
                                                     │
               ┌─────────────── namespace aegisvolt-<env> (Argo Rollouts, canary) ────────────┐
               │  graphql-gateway  (GraphQL + ORQUESTADOR SAGA)                                │
               │      │ HTTP + X-Correlation-ID (= saga_id)                                    │
               │      ├──► station-reservation-service ──► DynamoDB aegisvolt-reservations-env │
               │      ├──► billing-wallet-service      ──► DynamoDB aegisvolt-billing-env      │
               │      └──► energy-dispatch-service     ──► DynamoDB aegisvolt-dispatch-env     │
               │  gateway ──► aegisvolt-sagas-env (log SAGA)  ·  todos ──► aegisvolt-flags-env │
               └──────────────────────────────────────────────────────────────────────────────┘
   Logs JSON ──► CloudWatch /aegisvolt/<env>/<servicio>      Métricas ──► AegisVolt/<env>
   Prefect (cada 5 min) ──► salud de los 3 ambientes + auditoría de consistencia SAGA
```

| Capa | Tecnología | Dónde |
|---|---|---|
| IaC | Terraform 1.15, workspaces dev/test/prod, backend S3 versionado | `infra/` |
| Nube | Floci 2.5 (emulador AWS persistente) en `localhost:4566` | `scripts/floci-up.sh` |
| Red | VPC por ambiente, subredes públicas y privadas, Gateway VPCE (S3, DynamoDB), Interface VPCE (execute-api, logs, monitoring, sts) | `infra/modules/network` |
| Datos | DynamoDB: Database-per-Service por ambiente | `infra/modules/dynamodb` |
| Backend | Python 3.13, FastAPI, Strawberry GraphQL | `services/` |
| Contenedores | Docker (una imagen por microservicio) | `services/Dockerfile` |
| Orquestación | kind (sustituye a EKS, que también está declarado en Terraform), ingress-nginx, Argo Rollouts | `k8s/` |
| Frontend | React 19 + Vite + TypeScript, servido por nginx | `frontend/` |
| Observabilidad | CloudWatch Logs/Metrics/Alarms + Prefect 3 | `infra/modules/observability`, `observability/prefect` |
| CI/CD | GitHub Actions (checks pre-merge + deploy post-merge) | `.github/workflows` |

## Estructura

```
infra/bootstrap      bucket S3 versionado para los tfstate (estado local, se ejecuta 1 vez)
infra/live           módulo raíz: backend S3 + workspaces; nombra todo con terraform.workspace
infra/modules/       network · dynamodb · observability · apigateway · eks
services/            aegis_common (logging, flags, métricas, canary) + 4 microservicios
k8s/base             Rollouts canary, Services stable/canary, AnalysisTemplate, Ingress
k8s/overlays/<env>   namespace, ConfigMap, host y NodePort por ambiente
frontend/            UI React
observability/       flows de Prefect
tests/               unit (moto) · integration (Floci) · e2e (ambiente desplegado)
scripts/             levantar plataforma, build, deploy canary, demos, restauración de tfstate
```

## Puesta en marcha (desde cero)

Requisitos: docker, terraform, kubectl, kind, helm, node, uv, aws-cli (perfil `floci`).

```bash
make up                       # Floci + kind (ingress, Argo Rollouts) + Prefect
make bootstrap                # bucket S3 versionado de estados
make tf-init
for e in dev test prod; do terraform -chdir=infra/live workspace select -or-create $e; make tf-apply ENV=$e; done
for e in dev test prod; do make deploy ENV=$e TAG=v1; done
```

| URL | Qué es |
|---|---|
| http://dev.aegisvolt.localhost | Frontend de dev (también `test.` y `prod.`) |
| http://dev.aegisvolt.localhost/graphql | GraphiQL / API GraphQL de dev |
| http://localhost:4566/execute-api/&lt;api-id&gt;/dev/graphql | La misma API a través del API Gateway (`terraform output api_gateway_invoke_url`) |
| http://localhost:4200 | Dashboard de Prefect |

## Pruebas

```bash
make test-unit            # 36 pruebas: pricing, telemetría, orquestador SAGA, idempotencia de servicios
make test-integration     # 9 pruebas: GraphQL -> SAGA -> microservicios -> DynamoDB real (Floci)
make test-e2e ENV=dev     # 8 pruebas: frontend, ingress, SAGA completa, fallos, API Gateway
```

## Flujo de promoción

`feature/*` ──PR──► `dev` ──PR──► `test` ──PR──► `prod`

Cada PR ejecuta `promotion-guard`, `lint-build`, `unit-tests`, `integration-tests`, `e2e-tests`
y `terraform-plan` (este último sobre el workspace de la rama destino). Cada merge dispara
`deploy.yml`: `terraform apply` en el workspace de esa rama, seguido del canary en su namespace.
Las ramas se protegen con `scripts/github/protect-branches.sh`.
