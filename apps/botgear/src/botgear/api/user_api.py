# managed
from botgear.api.crud_router_factory import create_crud_router
from botgear.models.user import User


router = create_crud_router(
    model_cls=User,
    resource_slug="user",
    service_resource="user",
    service_module="botgear.services.user_service",
    roles=['Admin'],
    methods=None,
)
