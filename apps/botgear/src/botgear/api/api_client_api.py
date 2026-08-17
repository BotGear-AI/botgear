# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.api_client import ApiClient


router = create_crud_router(
    model_cls=ApiClient,
    resource_slug="api-client",
    service_resource="api_client",
    service_module="botgear.services.api_client_service",
    roles=['Admin'],
    methods=['create', 'list', 'get_by_id', 'update', 'delete'],
)
