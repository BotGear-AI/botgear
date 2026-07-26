# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.service_proxy import ServiceProxy


router = create_crud_router(
    model_cls=ServiceProxy,
    resource_slug="service-proxy",
    service_resource="service_proxy",
    service_module="botcore.services.service_proxy_service",
    roles=['Admin'],
    methods=None,
)
