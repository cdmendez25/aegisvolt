# AegisVolt — comandos frecuentes.  `make help` para ver la lista.
SHELL := /bin/bash
ENV ?= dev
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo local)
TF  := terraform -chdir=infra/live

.PHONY: help
help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

# ---------------- plataforma local ----------------
up: ## Levanta Floci, el clúster kind (ingress + Argo Rollouts) y Prefect
	./scripts/floci-up.sh && ./scripts/cluster-up.sh && ./scripts/prefect-up.sh

bootstrap: ## Crea el bucket S3 versionado de los tfstate (una sola vez)
	terraform -chdir=infra/bootstrap init -input=false && terraform -chdir=infra/bootstrap apply -auto-approve

# ---------------- infraestructura ----------------
tf-init: ## terraform init del módulo raíz (backend S3)
	$(TF) init -input=false

tf-plan: ## terraform plan del workspace ENV  (make tf-plan ENV=test)
	$(TF) workspace select $(ENV) && $(TF) plan

tf-apply: ## terraform apply del workspace ENV
	$(TF) workspace select $(ENV) && $(TF) apply -auto-approve

tf-state-versions: ## Lista las versiones (backups) del tfstate de ENV en S3
	AWS_PROFILE=floci aws s3api list-object-versions --bucket aegisvolt-tfstate \
	  --prefix "env:/$(ENV)/aegisvolt/terraform.tfstate" \
	  --query 'Versions[?ends_with(Key,`.tfstate`)].[VersionId,LastModified,IsLatest,Size]' --output table

# ---------------- build / deploy ----------------
build: ## Construye las imágenes Docker con TAG
	./scripts/build-images.sh $(TAG)

deploy: build ## Build + despliegue canary en ENV  (make deploy ENV=test)
	./scripts/deploy.sh $(ENV) $(TAG)

rollouts: ## Estado de los Rollouts canary en ENV
	kubectl -n aegisvolt-$(ENV) get rollouts.argoproj.io,analysisruns.argoproj.io

# ---------------- pruebas ----------------
lint: ## Ruff + formato + type-check del frontend + terraform fmt
	uv run ruff check services tests scripts observability && uv run ruff format --check services tests
	cd frontend && npm run lint
	terraform fmt -check -recursive infra

test-unit: ## Pruebas unitarias
	uv run pytest tests/unit -v

test-integration: ## Pruebas de integración contra Floci
	uv run pytest tests/integration -m integration -v

test-e2e: ## Pruebas E2E contra el ambiente desplegado ENV
	E2E_ENV=$(ENV) APIGW_URL="$$($(TF) output -raw api_gateway_invoke_url)" uv run pytest tests/e2e -m e2e -v

# ---------------- demos para la sustentación ----------------
demo-fault-on: ## Activa SIMULATE_CHARGER_HARDWARE_FAULT en ENV (sin redeploy)
	AWS_PROFILE=floci aws dynamodb update-item --table-name aegisvolt-flags-$(ENV) \
	  --key '{"flag_name":{"S":"SIMULATE_CHARGER_HARDWARE_FAULT"}}' \
	  --update-expression 'SET enabled = :t, updated_by = :u' \
	  --expression-attribute-values '{":t":{"BOOL":true},":u":{"S":"aws-cli"}}'

demo-fault-off: ## Desactiva la flag de falla en ENV
	AWS_PROFILE=floci aws dynamodb update-item --table-name aegisvolt-flags-$(ENV) \
	  --key '{"flag_name":{"S":"SIMULATE_CHARGER_HARDWARE_FAULT"}}' \
	  --update-expression 'SET enabled = :f, updated_by = :u' \
	  --expression-attribute-values '{":f":{"BOOL":false},":u":{"S":"aws-cli"}}'

flags: ## Muestra las flags de los 3 ambientes
	@for e in dev test prod; do printf "%-5s " $$e; AWS_PROFILE=floci aws dynamodb scan --table-name aegisvolt-flags-$$e \
	  --query 'Items[].[flag_name.S,enabled.BOOL]' --output text | tr '\t\n' '= '; echo; done

trace: ## Rastrea una SAGA en CloudWatch: make trace ID=<correlation_id> ENV=dev
	uv run scripts/trace_saga.py $(ID) --env $(ENV)

canary-fail: ## Despliega una revisión defectuosa => rollback automático
	./scripts/canary-demo.sh fail $(ENV)

canary-restore: ## Restaura la revisión sana tras la demo de canary
	./scripts/canary-demo.sh restore $(ENV)

orphan: ## Crea una SAGA huérfana para que Prefect la detecte y recupere
	uv run scripts/simulate_orphan.py $(ENV)

prefect-run: ## Ejecuta ya el flow de observabilidad en Prefect
	docker exec -e PREFECT_API_URL=http://prefect-server:4200/api prefect-observability-flow \
	  prefect deployment run 'aegisvolt-observability/cada-5-minutos'

reset: ## Libera conectores, restaura saldos y apaga la flag de falla en ENV
	uv run scripts/reset_demo.py $(ENV)
