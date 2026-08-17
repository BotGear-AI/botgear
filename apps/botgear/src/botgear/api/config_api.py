# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.config import Config


router = create_crud_router(
    model_cls=Config,
    resource_slug="config",
    service_resource="config",
    service_module="botgear.services.config_service",
    roles=['Admin'],
    methods=None,
)
