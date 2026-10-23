"""Wiring: one place that builds the services from settings, so the API, the CLI
and the tests all assemble the same objects."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.ai.investigation import InvestigationProvider, provider_from_settings
from app.clock import Clock, clock_from_settings
from app.config import Settings
from app.db.session import make_engine, make_session_factory
from app.ingestion.conflicts import ConflictService
from app.ingestion.datasets import DatasetRegistry
from app.ingestion.service import ImportService
from app.pipeline import ReconciliationService
from app.workflows.service import TaskWorkflowService


@dataclass
class Services:
    settings: Settings
    clock: Clock
    engine: Engine
    sessions: sessionmaker[Session]
    registry: DatasetRegistry
    ai_provider: InvestigationProvider
    imports: ImportService = field(init=False)
    conflicts: ConflictService = field(init=False)
    reconciliation: ReconciliationService = field(init=False)
    workflow: TaskWorkflowService = field(init=False)

    def __post_init__(self) -> None:
        tenant = self.settings.tenant_id
        self.imports = ImportService(self.sessions, tenant, self.clock, self.registry)
        self.conflicts = ConflictService(self.sessions, tenant, self.clock, self.registry)
        self.reconciliation = ReconciliationService(self.sessions, tenant, self.clock, self.settings, self.registry)
        self.workflow = TaskWorkflowService(
            self.sessions, tenant, self.clock, timedelta(minutes=self.settings.availability_max_age_minutes)
        )

    @property
    def tenant_id(self) -> str:
        return self.settings.tenant_id


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    registry: DatasetRegistry | None = None,
    ai_provider: InvestigationProvider | None = None,
    engine: Engine | None = None,
) -> Services:
    engine = engine or make_engine(settings.database_url)
    return Services(
        settings=settings,
        clock=clock or clock_from_settings(settings),
        engine=engine,
        sessions=make_session_factory(engine),
        registry=registry or DatasetRegistry(),
        ai_provider=ai_provider or provider_from_settings(settings.ai_provider, settings.anthropic_model),
    )
