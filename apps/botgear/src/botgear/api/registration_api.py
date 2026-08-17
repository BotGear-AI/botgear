# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.registration import Registration


router = create_crud_router(
    model_cls=Registration,
    resource_slug="registration",
    service_resource="registration",
    service_module="botgear.services.registration_service",
    roles=['Admin'],
    methods=None,
)
