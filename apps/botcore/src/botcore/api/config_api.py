# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.config import Config


router = create_crud_router(
    model_cls=Config,
    resource_slug="config",
    service_resource="config",
    service_module="botcore.services.config_service",
    roles=['Admin'],
    methods=None,
)
