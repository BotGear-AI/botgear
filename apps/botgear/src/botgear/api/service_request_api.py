# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.service_request import ServiceRequest


router = create_crud_router(
    model_cls=ServiceRequest,
    resource_slug="service-request",
    service_resource="service_request",
    service_module="botgear.services.service_request_service",
    roles=['Admin'],
    methods=None,
)
