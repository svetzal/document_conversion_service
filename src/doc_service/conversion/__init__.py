"""
Domain layer for document conversion.
Provides interfaces (gateways) and a service to orchestrate conversion jobs,
abstracting I/O, security, and docling so front-ends (HTTP or others) can
use the same core logic.
"""

from .interfaces import ConverterGateway, StorageGateway, SecurityGateway
from .service import ConversionService, JobRecord, JobStatus
