# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.service_meta import ServiceMeta


router = create_crud_router(
    model_cls=ServiceMeta,
    resource_slug="service-meta",
    service_resource="service_meta",
    service_module="botgear.services.service_meta_service",
    roles=['Admin'],
    methods=None,
)
