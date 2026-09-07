"""Canonical enterprise-WeChat callback boundary.

Only ``/wechat`` is registered in the admin application.  The tested callback
implementation remains shared internally without exposing a second URL.
"""

from fastapi import APIRouter

from ..routes import wecom_callback_receive, wecom_callback_verify

router = APIRouter(tags=["wechat"])
router.add_api_route("/wechat", wecom_callback_verify, methods=["GET"], include_in_schema=False)
router.add_api_route("/wechat", wecom_callback_receive, methods=["POST"], include_in_schema=False)

__all__ = ["router"]
