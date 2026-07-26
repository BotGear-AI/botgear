# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.workspace import Workspace


router = create_crud_router(
    model_cls=Workspace,
    resource_slug="workspace",
    service_resource="workspace",
    service_module="botcore.services.workspace_service",
    roles=['Admin'],
    methods=None,
)
