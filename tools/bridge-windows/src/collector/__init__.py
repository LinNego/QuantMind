"""通达信行情采集器（独立进程）：全市场 pricevol + 热集 snapshot + 1m 聚合。"""
from .config import VERSION, CollectorConfig, load_config
from .service import CollectorService

__all__ = ["VERSION", "CollectorConfig", "load_config", "CollectorService"]
