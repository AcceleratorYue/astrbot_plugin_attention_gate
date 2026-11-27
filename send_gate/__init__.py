"""统一消息发送门控的 dry-run 观察层。

当前阶段只观察，不延迟、不停止、不修改任何真实请求。
"""

from .dryrun import SendGateObserver
from .request_kind import RequestInfo, classify_request

__all__ = ["SendGateObserver", "RequestInfo", "classify_request"]
