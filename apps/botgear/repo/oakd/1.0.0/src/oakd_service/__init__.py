"""oakd_service — botgear subprocess service for Luxonis OAK-D depth
cameras (DepthAI V2 API). RGB + on-device stereo depth streaming."""
from .service import OakdService

__all__ = ["OakdService"]
