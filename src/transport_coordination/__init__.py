"""交通协同服务端基础包：基础登记能力与社会资本准入退出域。"""

from .admission_service import AdmissionService
from .service import DomainService

__all__ = ["DomainService", "AdmissionService"]
