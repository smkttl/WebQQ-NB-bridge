"""NoneBot plugin entry point for the standalone WebQQ bridge."""

from .service import BridgeService
from . import inspect

__plugin_meta__ = {
    "name": "WebQQ Bridge",
    "description": "Expose an official QQ bot through WebQQ's OneBot backend.",
    "usage": "Configure WEBQQ_BRIDGE_* environment variables and a mapping JSON file.",
    "type": "application",
    "homepage": "https://github.com/smkttl/WebQQ-NB-bridge",
}

service = BridgeService()
try:
    service.register_hooks()
except ValueError:
    # Unit tests may import the package before NoneBot initializes its driver.
    # NoneBot's normal plugin loader initializes the driver first.
    pass

__all__ = ["BridgeService", "inspect", "service"]
