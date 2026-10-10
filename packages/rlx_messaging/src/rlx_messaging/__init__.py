"""rlx_messaging — shared `messaging` capability base for botgear.

See packages/rlx_messaging/README.md for the full design.

Public surface:

    MessagingServiceBase   Subclass + implement _connect/_disconnect/_send
                            (+ optionally _list_channels) to get the full
                            listen/speak/send/list_channels contract.
    MessagingConfig         Common config fields (default_channel,
                            echo_own_messages) every platform config
                            subclasses.
"""
from .base import InboxMessage, MessagingConfig, MessagingServiceBase, MessagingState, SpokenMessage

__all__ = ["MessagingConfig", "MessagingServiceBase", "MessagingState", "InboxMessage", "SpokenMessage"]
__version__ = "0.1.0"
