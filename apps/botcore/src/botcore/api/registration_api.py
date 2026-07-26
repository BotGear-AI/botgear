# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.registration import Registration


router = create_crud_router(
    model_cls=Registration,
    resource_slug="registration",
    service_resource="registration",
    service_module="botcore.services.registration_service",
    roles=['Admin'],
    methods=None,
)
