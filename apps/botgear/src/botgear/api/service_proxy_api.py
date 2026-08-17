# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.service_proxy import ServiceProxy


router = create_crud_router(
    model_cls=ServiceProxy,
    resource_slug="service-proxy",
    service_resource="service_proxy",
    service_module="botgear.services.service_proxy_service",
    roles=['Admin'],
    methods=None,
)
