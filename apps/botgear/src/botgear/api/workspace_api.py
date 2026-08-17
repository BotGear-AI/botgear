# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.workspace import Workspace


router = create_crud_router(
    model_cls=Workspace,
    resource_slug="workspace",
    service_resource="workspace",
    service_module="botgear.services.workspace_service",
    roles=['Admin'],
    methods=None,
)
