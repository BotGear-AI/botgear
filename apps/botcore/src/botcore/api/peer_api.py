# managed
from botcore.api.crud_router_factory import create_crud_router
from botcore.models.peer import Peer


router = create_crud_router(
    model_cls=Peer,
    resource_slug="v1/peers",
    service_resource="peer",
    service_module="botcore.services.peer_service",
    roles=['Admin'],
    methods=['list', 'request'],
    list_at_root=True,
)
