# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.link import Link


router = create_crud_router(
    model_cls=Link,
    resource_slug="v1/links",
    service_resource="link",
    service_module="botcore.services.link_service",
    roles=['Admin'],
    methods=['list', 'request'],
    list_at_root=True,
)
