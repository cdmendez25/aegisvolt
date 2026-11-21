import os
from dataclasses import dataclass

from fastapi import Request
from strawberry.fastapi import GraphQLRouter

from aegis_common.app import create_app
from aegis_common.config import Settings
from aegis_common.flags import FlagClient

from .clients import Services
from .saga import SagaOrchestrator
from .schema import schema
from .store import DynamoSagaStore

app, settings, log, flags = create_app(
    "graphql-gateway", "AegisVolt GraphQL Gateway", readiness_tables=["sagas", "flags"]
)


@dataclass
class Deps:
    settings: Settings
    services: Services
    store: DynamoSagaStore
    orchestrator: SagaOrchestrator
    flags: FlagClient
    flag_admin_enabled: bool


def build_deps(services: Services | None = None) -> Deps:
    services = services or Services.from_env()
    store = DynamoSagaStore(settings)
    return Deps(
        settings=settings,
        services=services,
        store=store,
        orchestrator=SagaOrchestrator(services, store, settings.env),
        flags=flags,
        flag_admin_enabled=os.environ.get("FLAG_ADMIN_ENABLED", "false") == "true",
    )


deps = build_deps()


async def get_context(request: Request = None):  # type: ignore[assignment]
    return {"deps": app.state.deps if hasattr(app.state, "deps") else deps, "request": request}


graphql_router = GraphQLRouter(schema, context_getter=get_context, graphql_ide="graphiql")
app.include_router(graphql_router, prefix="/graphql")


@app.get("/schema.graphql", tags=["ops"])
def sdl():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(schema.as_str())
