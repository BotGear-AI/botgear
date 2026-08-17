# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.script import Script


router = create_crud_router(
    model_cls=Script,
    resource_slug="script",
    service_resource="script",
    service_module="botgear.services.script_service",
    roles=['Admin'],
    methods=None,
)
