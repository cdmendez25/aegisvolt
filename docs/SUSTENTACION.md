# Guía de sustentación · AegisVolt

Cada sección responde una pregunta del banco del profesor con **(1) la respuesta corta**,
**(2) dónde está en el código** y **(3) la demo exacta**. Los comandos se ejecutan desde la
raíz del repo.

## 0. Checklist antes de entrar (5 min)

```bash
docker start floci prefect-server prefect-observability-flow   # si reiniciaste el PC
kubectl get nodes                                              # kind arranca solo con docker
make flags                                                     # flags de los 3 ambientes
for e in dev test prod; do make reset ENV=$e; done             # conectores libres, saldos semilla
```

Deja abiertos: http://dev.aegisvolt.localhost, http://prod.aegisvolt.localhost y http://localhost:4200

---

## 1. Infraestructura como Código (30%)

### 1.1 «¿Cómo garantiza que `terraform apply` en test no sobrescriba las tablas ni el estado de prod?»

**Respuesta:** por tres mecanismos independientes.

1. **Estado separado por workspace.** El backend S3 guarda cada workspace en su propia clave:
   `env:/dev/...`, `env:/test/...` y `env:/prod/aegisvolt/terraform.tfstate`. Un apply en `test`
   solo lee y escribe `env:/test/...`, así que *ni siquiera conoce* los recursos de prod.
   Ver `infra/live/backend.tf` (`workspace_key_prefix = "env:"`).
2. **Nombres derivados del workspace.** En `infra/live/main.tf` se define `env = terraform.workspace`
   y todo se nombra con eso: `aegisvolt-billing-test` frente a `aegisvolt-billing-prod`. **No existe
   una variable `env`**, así que es imposible aplicar la config de prod desde el workspace test.
   Si alguien está en el workspace `default`, el plan falla porque no hay entrada en `env_config`.
3. **Protección extra en prod.** Las tablas de prod tienen `deletion_protection_enabled = true` y PITR.
   El pipeline además verifica `workspace show == rama` antes del apply (`deploy.yml`).
   El lock (`use_lockfile`) impide dos applies simultáneos sobre el mismo workspace.

**Demo:**
```bash
terraform -chdir=infra/live workspace list
terraform -chdir=infra/live workspace select test && terraform -chdir=infra/live plan   # "No changes"
terraform -chdir=infra/live state list | grep aws_dynamodb_table.service                # solo recursos de test
AWS_PROFILE=floci aws s3 ls s3://aegisvolt-tfstate --recursive | grep tfstate$
```

### 1.2 «Muestre las versiones anteriores del tfstate de dev y explique cómo restauraría la previa»

**Respuesta:** el bucket tiene `versioning = Enabled` (`infra/bootstrap/main.tf`). Cada apply
crea una versión nueva y las anteriores quedan como backup. Para restaurar, copio la versión
buena sobre la misma clave. S3 crea una versión *nueva* con ese contenido y la corrupta no se
borra, así que queda auditoría. Subo el `serial` para que Terraform acepte el estado, y después
corro `terraform plan` para ver la diferencia con la infraestructura real.

**Demo:**
```bash
make tf-state-versions ENV=dev
./scripts/restore-tfstate.sh dev                  # lista versiones
./scripts/restore-tfstate.sh dev <VERSION_ID>     # dry-run: compara serial y número de recursos
./scripts/restore-tfstate.sh dev <VERSION_ID> --apply
```
Manualmente sería: `aws s3api get-object --version-id <id> ...` y luego `put-object`.

### 1.3 «Explique el recorrido de un paquete desde el API Gateway hasta el pod a través del VPCE»

**Respuesta (AWS real, `enable_eks = true`):**

1. Un cliente dentro de la VPC llama a `https://<api-id>.execute-api.us-east-1.amazonaws.com`.
   Gracias a `private_dns_enabled`, ese nombre resuelve a las **IPs privadas del Interface VPCE
   `execute-api`**, que tiene ENIs en las subredes privadas.
2. El **SG `vpce`** solo admite TCP 443 desde el CIDR de la VPC.
3. El API Gateway es de tipo **PRIVATE**. Su **resource policy** niega todo lo que no venga con
   `aws:SourceVpce = <id del VPCE>`.
4. La integración `HTTP_PROXY` usa un **VPC Link** hacia un **NLB interno** en las subredes privadas.
5. El NLB reenvía al **NodePort** del gateway (30081, 30082 o 30083 según el ambiente) en los nodos
   EKS. El **SG `k8s_nodes`** solo admite ese puerto desde los CIDR de las subredes privadas.
6. **kube-proxy** lleva el tráfico del Service `graphql-gateway-nodeport` al pod.
7. Desde el pod, las llamadas a DynamoDB y S3 salen por los **Gateway Endpoints**: las tablas de
   ruteo privadas tienen la ruta al *prefix list* del servicio. Las llamadas a CloudWatch salen por
   los Interface VPCE `logs` y `monitoring`. **Las subredes privadas no tienen ruta 0.0.0.0/0 al
   IGW**, así que nada sale a internet.

Tablas de ruteo: `rt-private-*` (ruta local más los prefix lists de los gateway endpoints), y
`rt-public` (0.0.0.0/0 → IGW, solo para las subredes públicas).
El código está en `infra/modules/network/main.tf` y `infra/modules/apigateway/main.tf`.

**Demo:**
```bash
AWS_PROFILE=floci aws ec2 describe-vpc-endpoints --filters Name=tag:Environment,Values=dev \
  --query 'VpcEndpoints[].[VpcEndpointType,ServiceName,State]' --output table
AWS_PROFILE=floci aws ec2 describe-route-tables --filters Name=tag:Environment,Values=dev \
  --query 'RouteTables[].[Tags[?Key==`Name`]|[0].Value,Routes[].DestinationCidrBlock|join(`,`,@)]' --output table
curl -s "$(terraform -chdir=infra/live output -raw api_gateway_invoke_url)" \
  -H 'content-type: application/json' -d '{"query":"{ environment { name } }"}'   # API GW -> NodePort -> pod
```
> En Floci el emulador no enruta por VPC Link (`enable_vpc_link = false`), así que la integración
> va directa al NodePort del nodo kind. La topología completa queda declarada en Terraform.

---

## 2. CI/CD, gobernanza y canary (25%)

### 2.1 «Intente un `git push --force` o un push directo a test»

**Respuesta:** el *ruleset* `aegisvolt-ambientes-protegidos` (`scripts/github/protect-branches.sh`)
aplica a dev, test y prod sin *bypass*, ni siquiera para el administrador:
- `non_fast_forward` bloquea el force push.
- `pull_request` bloquea el push directo; todo entra por PR.
- `required_status_checks` exige los 6 checks en verde.
- `deletion` impide borrar las ramas.

**Demo:**
```bash
git checkout test && git commit --allow-empty -m "intento directo" && git push origin test
#   -> remote: error: GH013: Repository rule violations ... Changes must be made through a pull request
git push --force origin test
#   -> ... Cannot force-push to this branch
git reset --hard origin/test
```

### 2.2 «Explique en el YAML cómo se determina el ambiente para `terraform apply` tras el merge»

**Respuesta:** `.github/workflows/deploy.yml` se dispara con `on: push: branches: [dev, test, prod]`,
que es el push que genera el merge del PR. La rama destino llega en `github.ref_name`:
`TARGET_ENV: ${{ github.ref_name }}`. Con eso se ejecuta `terraform workspace select "$TARGET_ENV"`,
se verifica con `workspace show` y se hace `apply -auto-approve`. Después se despliega en el
namespace `aegisvolt-$TARGET_ENV`. Además, `concurrency: deploy-<rama>` evita dos despliegues
simultáneos al mismo ambiente. En los PR (`pr-checks.yml`), el job `terraform-plan` usa
`github.base_ref`, es decir, la rama **destino** del PR.

### 2.3 «¿Qué métrica evalúa el canary antes de pasar del 20% al 100%?»

**Respuesta:** el paso `analysis` del Rollout (`k8s/base/*.yaml`) ejecuta el AnalysisTemplate
`canary-health` (`k8s/base/analysis-template.yaml`). Ese template lanza un Job con
`services/aegis_common/canary_check.py`, que hace dos cosas:
1. Envía **30 peticiones** solo a los pods canary (Service `<svc>-canary`, que Argo apunta al hash
   nuevo): `/readyz` y un endpoint de negocio.
2. Consulta en **CloudWatch** las métricas `Requests` y `Http5xx` del namespace `AegisVolt/<env>` con
   la dimensión **`Revision = <hash canary>`**, que cada pod publica cada 10 s.

**Condición:** tasa de error ≤ **5%** en ambas mediciones. Si se cumple, se pasa a 60% y luego
a 100%. Si no, `exit 1`, el AnalysisRun queda `Failed`, Argo **aborta** y el 100% vuelve a
**stable** (rollback automático). El resultado queda en `/aegisvolt/<env>/canary-analysis`.

**Demo (rollback automático):**
```bash
make canary-fail ENV=dev          # revisión con 5xx inyectados -> AnalysisRun Failed -> rollback
kubectl argo rollouts get rollout station-reservation-service -n aegisvolt-dev
AWS_PROFILE=floci aws logs filter-log-events --log-group-name /aegisvolt/dev/canary-analysis \
  --filter-pattern '{ $.event = "CANARY_ANALYSIS_RESULT" }' --query 'events[-1].message' --output text
make canary-restore ENV=dev
```
Resultado real medido: `cloudwatch_error_rate 0.7` frente a un umbral de `0.05`, con 14 errores 5xx de 20 peticiones.

---

## 3. Backend, SAGA y Feature Flags (25%)

### 3.1 «Active la flag de fallo en test, dispare la mutación y muestre la compensación en DynamoDB»

```bash
make demo-fault-on ENV=test            # cambia la flag en DynamoDB; los pods la leen en ≤3 s
curl -s http://test.aegisvolt.localhost/graphql -H 'content-type: application/json' -d '{"query":
 "mutation { reserveAndStartCharging(input:{stationId:\"BOG-01\",connectorId:\"C1\",fleetId:\"FLEET-ANDES\",energyKwh:20}) { sagaId status failedStep errorCode steps { name action status } } }"}'
# -> status COMPENSATED, START_CHARGING FAILED, REFUND_WALLET y RELEASE_CONNECTOR SUCCEEDED
SAGA=<sagaId>
AWS_PROFILE=floci aws dynamodb query --table-name aegisvolt-billing-test \
  --key-condition-expression 'pk = :p AND begins_with(sk, :s)' \
  --expression-attribute-values "{\":p\":{\"S\":\"WALLET#FLEET-ANDES\"},\":s\":{\"S\":\"TX#$SAGA\"}}" \
  --query 'Items[].[sk.S,amount.N,status.S,reason.S]' --output table   # CHARGE REFUNDED + REFUND
AWS_PROFILE=floci aws dynamodb get-item --table-name aegisvolt-reservations-test \
  --key '{"pk":{"S":"STATION#BOG-01"},"sk":{"S":"CONNECTOR#C1"}}' \
  --query 'Item.[status.S,last_released_by.S]'                         # AVAILABLE, liberado por la saga
make demo-fault-off ENV=test
```
También se puede hacer desde la UI de test: interruptor «Simular falla de cargador», elegir un
conector y pulsar *Reservar*. El historial muestra las compensaciones ↩.

**Orden de compensación:** si falla el paso 3, primero el reembolso (billing) y luego la
liberación del conector (reservas), es decir, el orden inverso de ejecución. Si falla el paso 2
(fondos insuficientes, flota `FLEET-SINSALDO`), solo se libera el conector.

### 3.2 «¿Cómo evita problemas de idempotencia si una compensación se procesa dos veces?»

**Respuesta:** en cuatro niveles.
1. **Clave natural por SAGA:** cada movimiento se guarda con `sk = TX#<saga_id>#CHARGE|REFUND`.
   El reembolso usa una `TransactWriteItems` con `attribute_not_exists(pk)` sobre el REFUND,
   más la condición `status = CAPTURED` sobre el CHARGE. Si llega dos veces, la transacción se
   **cancela entera** y se devuelve el reembolso original (`"idempotent": true`); el saldo nunca
   se suma dos veces.
2. **Liberación condicional:** el conector solo vuelve a AVAILABLE si `reserved_by = saga_id`.
   Una compensación tardía o duplicada **no libera el conector de otra SAGA**.
3. **Compensaciones no-op:** compensar un paso que nunca ocurrió responde `NOOP`. Por eso, ante un
   timeout (resultado incierto) el orquestador compensa sin miedo.
4. **Idempotencia de la mutación:** `saga_id = uuid5(idempotencyKey)`. Reintentar con la misma
   clave devuelve la misma SAGA sin volver a llamar a los servicios.

Las compensaciones se reintentan con backoff exponencial (4 intentos). Si todos fallan, la SAGA
queda en `COMPENSATION_FAILED`, se emite una alarma y Prefect la reporta.
Pruebas: `tests/unit/test_services.py::test_refund_twice_only_refunds_once` y
`test_release_never_frees_connector_of_another_saga`.

**Demo:**
```bash
# billing no tiene ruta pública (solo el gateway lo llama): se demuestra con las pruebas
uv run pytest tests/unit -k "twice or idempot or another_saga" -v
```

### Otras preguntas probables
- **¿Orquestación o coreografía?** Orquestación. El coordinador vive en la capa GraphQL
  (`services/graphql_gateway/saga.py`) y persiste cada paso en `aegisvolt-sagas-<env>`.
  Ventaja: el flujo y su estado son explícitos y auditables.
- **Desacople:** cada servicio es **dueño único** de su tabla, y la política IAM por servicio solo
  le da acceso a la suya (`infra/modules/eks`). Se comunican solo por HTTP. Cada uno tiene su propia
  imagen y su propio Rollout canary.
- **Esquema GraphQL:** `curl http://localhost:30081/schema.graphql` (SDL completo), o GraphiQL en
  `/graphql`. Expone queries, mutations y una subscription WSS (`stationsUpdated`).

---

## 4. Observabilidad (12%)

### 4.1 «Muestre el flujo en Prefect y rastree en CloudWatch el correlation_id de una SAGA fallida»

```bash
make prefect-run              # o en http://localhost:4200 -> Deployments -> cada-5-minutos -> Run
# En la UI: flow run -> tasks health-<env>, audit-sagas-<env>, publish-metrics-<env>;
#           pestaña Artifacts -> tabla "aegisvolt-environments" y reporte "aegisvolt-saga-audit"
make orphan ENV=dev && make prefect-run      # detecta la huérfana y la recupera (COMPENSATED)

make trace ID=<sagaId de la SAGA fallida> ENV=test
# filtro: { $.correlation_id = "<id>" } en /aegisvolt/test/* -> línea de tiempo de los 4 servicios
```
Cada log es JSON con `timestamp, level, service, env, version, revision, pod, correlation_id, event`.
El `correlation_id` es el `saga_id` y viaja en el header `X-Correlation-ID`.

## 5. Frontend (8%)

### 5.1 «Cambie una flag en dev sin redesplegar; dev cambia y prod no»

1. Abre http://dev.aegisvolt.localhost y http://prod.aegisvolt.localhost lado a lado.
2. En dev, apaga «Tarifa dinámica (surge)» o ejecuta `make demo-fault-on ENV=dev`.
3. En ≤2 s la UI de dev refleja el cambio: el precio pasa de ×1.5 a la tarifa base. Prod sigue
   igual porque **lee otra tabla** (`aegisvolt-flags-prod`).
4. `kubectl -n aegisvolt-dev get pods` muestra que **no se reinició ningún pod**: las flags se
   leen de DynamoDB con caché de 3 s. Terraform no las revierte gracias a `ignore_changes = [item]`.
5. En prod los interruptores están en **solo lectura** (`FLAG_ADMIN_ENABLED=false`), como
   gobernanza del ambiente productivo.
