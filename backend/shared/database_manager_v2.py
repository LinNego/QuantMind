"""
企业级数据库连接池管理
支持主从读写分离、连接池、健康检查
"""

import asyncio
import logging
import os
import threading
import urllib.parse
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from collections.abc import AsyncGenerator

try:
    # 确保优先加载项目根目录的 .env（仅当 python-dotenv 可用）
    # 在容器化环境中，我们禁用此处的 load_dotenv，以避免干扰由 Docker 注入的统一变量。
    pass
    # from dotenv import load_dotenv  # type: ignore
    # _ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
    # if _ROOT_ENV.exists():
    #     load_dotenv(str(_ROOT_ENV), override=False)
except Exception:
    # 忽略加载失败，继续使用系统环境变量
    pass

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


class DatabaseConfig:
    """数据库配置"""

    def __init__(self):
        # 1. 优先从 DATABASE_URL 加载（全路径连接模式）
        self.database_url = os.getenv("DATABASE_URL", "").strip()

        # 2. 备选：从散装环境变量加载（独立服务模式）
        self.master_host = os.getenv("DB_MASTER_HOST", os.getenv("DB_HOST", "127.0.0.1"))
        self.master_port = int(os.getenv("DB_MASTER_PORT", os.getenv("DB_PORT", "5432")))
        self.database = os.getenv("DB_NAME", "quantmind")
        self.username = os.getenv("DB_USER", "postgres")
        self.password = os.getenv("DB_PASSWORD", "")

        # 初始化从库列表
        self.slave_hosts = os.getenv("DB_SLAVE_HOSTS", "")
        self.slave_list = [
            (host.split(":")[0], int(host.split(":")[1])) for host in self.slave_hosts.split(",") if host
        ]

        if self.database_url:
            logger.info(f"Using DATABASE_URL: {self.database_url.split('@')[-1]}")  # 隐藏密码
        else:
            logger.info(f"Database direct connect mode: {self.master_host}:{self.master_port}/{self.database}")

        # 连接池配置
        self.pool_size = int(os.getenv("DB_POOL_SIZE", "20"))
        self.max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "30"))
        self.pool_timeout = float(os.getenv("DB_POOL_TIMEOUT", "30"))
        self.pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "3600"))
        self.pool_pre_ping = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"

        # 连接配置
        self.echo = os.getenv("DB_ECHO", "false").lower() == "true"
        self.echo_pool = os.getenv("DB_ECHO_POOL", "false").lower() == "true"

    def get_master_url(self) -> str:
        """获取主库连接URL"""
        import socket

        # 强制 IP 物理转换逻辑：彻底终结 gaierror
        def resolve_to_ip(host_str):
            try:
                # 如果已经是 IP 则直接返回，否则强制解析
                return socket.gethostbyname(host_str.strip())
            except Exception:
                return host_str.strip()

        if self.database_url:
            url = self.database_url
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url

        user = urllib.parse.quote(self.username)
        password = urllib.parse.quote(self.password)

        # 物理修正解析
        host = resolve_to_ip(self.master_host)

        return f"postgresql+asyncpg://{user}:{password}@" f"{host}:{self.master_port}/{self.database}"

    def get_slave_url(self, host: str, port: int) -> str:
        """获取从库连接URL"""
        user = urllib.parse.quote(self.username)
        password = urllib.parse.quote(self.password)
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{self.database}"


class _LoopPools:
    """一个事件循环独占的引擎与会话工厂。

    asyncpg 连接绑定**创建它的事件循环**：把 A 循环的连接交给 B 循环用，
    SQLAlchemy 会抛 ``got Future attached to a different loop``（而且污染后
    连 uvicorn 主循环的普通请求都会一起失败）。所以池必须按 loop 隔离，
    绝不能在进程内共享一个 ``AsyncEngine``。
    """

    __slots__ = (
        "master_engine",
        "master_session_factory",
        "slave_engines",
        "slave_session_factories",
    )

    def __init__(self) -> None:
        self.master_engine: AsyncEngine | None = None
        self.master_session_factory: async_sessionmaker | None = None
        self.slave_engines: list[AsyncEngine] = []
        self.slave_session_factories: list[async_sessionmaker] = []


class DatabaseManager:
    """数据库连接池管理器（**按事件循环隔离**）"""

    def __init__(self, config: DatabaseConfig | None = None):
        self.config = config or DatabaseConfig()
        #: loop -> _LoopPools。WeakKeyDictionary：临时循环（asyncio.run）被回收时
        #: 条目自动消失，不会把死循环对象长期留在内存里。
        self._loop_pools: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, _LoopPools
        ] = weakref.WeakKeyDictionary()
        self._pools_lock = threading.Lock()
        self._current_slave_index = 0
        self._initialized = False

    @property
    def _master_engine(self) -> AsyncEngine | None:
        """当前事件循环的主库引擎（启动期 ``create_registered_tables`` 兼容口）。

        旧实现是实例属性；改成按 loop 取池后保留同名只读属性，避免调用点
        在 ``init_database()`` 之后拿到别家的引擎。非循环上下文返回 None。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        pools = self._loop_pools.get(loop)
        return pools.master_engine if pools else None

    def _pools_for_current_loop(self) -> _LoopPools:
        """取（必要时创建）当前事件循环的引擎与会话工厂。"""
        loop = asyncio.get_running_loop()
        with self._pools_lock:
            pools = self._loop_pools.get(loop)
            if pools is None:
                pools = self._create_pools()
                self._loop_pools[loop] = pools
                logger.info(
                    "Created async DB pools for event loop %s (master=%s)",
                    id(loop),
                    "yes" if pools.master_engine is not None else "no",
                )
            return pools

    def _create_pools(self) -> _LoopPools:
        pools = _LoopPools()

        # 创建主库引擎
        master_url = self.config.get_master_url()
        logger.info(f"Creating master database engine with URL: {master_url}")
        pools.master_engine = create_async_engine(
            master_url,
            pool_size=self.config.pool_size,
            max_overflow=self.config.max_overflow,
            pool_timeout=self.config.pool_timeout,
            pool_recycle=self.config.pool_recycle,
            pool_pre_ping=self.config.pool_pre_ping,
            echo=self.config.echo,
            echo_pool=self.config.echo_pool,
        )

        # 添加连接事件监听
        self._setup_engine_events(pools.master_engine, "master")

        # 创建主库会话工厂
        pools.master_session_factory = async_sessionmaker(
            pools.master_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # 创建从库引擎
        for idx, (host, port) in enumerate(self.config.slave_list, 1):
            try:
                slave_engine = create_async_engine(
                    self.config.get_slave_url(host, port),
                    pool_size=self.config.pool_size // 2,  # 从库连接池减半
                    max_overflow=self.config.max_overflow // 2,
                    pool_timeout=self.config.pool_timeout,
                    pool_recycle=self.config.pool_recycle,
                    pool_pre_ping=self.config.pool_pre_ping,
                    echo=self.config.echo,
                    echo_pool=self.config.echo_pool,
                )

                self._setup_engine_events(slave_engine, f"slave-{idx}")
                pools.slave_engines.append(slave_engine)

                # 创建从库会话工厂
                slave_session_factory = async_sessionmaker(
                    slave_engine,
                    class_=AsyncSession,
                    expire_on_commit=False,
                )
                pools.slave_session_factories.append(slave_session_factory)

                logger.info(f"Slave database {idx} ({host}:{port}) initialized")
            except Exception as e:
                logger.error(f"Failed to initialize slave database {idx}: {e}")

        logger.info(
            "Database connection pools initialized: "
            f"1 master, {len(pools.slave_engines)} slaves"
        )
        return pools

    async def initialize(self):
        """初始化当前事件循环的连接池并做健康检查。"""
        if self._initialized:
            logger.warning("DatabaseManager already initialized")
            return

        logger.info("Initializing database connection pools...")
        self._pools_for_current_loop()
        self._initialized = True

        # 执行健康检查
        await self.health_check()

    def _setup_engine_events(self, engine: AsyncEngine, name: str):
        """设置引擎事件监听"""

        @event.listens_for(engine.sync_engine, "connect")
        def receive_connect(dbapi_conn, connection_record):
            logger.debug(f"New connection to {name} database")

        @event.listens_for(engine.sync_engine, "checkout")
        def receive_checkout(dbapi_conn, connection_record, connection_proxy):
            logger.debug(f"Connection checked out from {name} pool")

        @event.listens_for(engine.sync_engine, "checkin")
        def receive_checkin(dbapi_conn, connection_record):
            logger.debug(f"Connection returned to {name} pool")

    @asynccontextmanager
    async def get_master_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取主库会话 (用于写操作)"""
        if not self._initialized:
            await self.initialize()

        pools = self._pools_for_current_loop()
        async with pools.master_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"Master session error: {e}")
                raise
            finally:
                await session.close()

    @asynccontextmanager
    async def get_slave_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取从库会话 (用于读操作，轮询负载均衡)"""
        if not self._initialized:
            await self.initialize()

        pools = self._pools_for_current_loop()
        # 如果没有从库，降级到主库
        if not pools.slave_session_factories:
            logger.warning("No slave databases available, using master for read")
            async with self.get_master_session() as session:
                yield session
                return

        # 轮询选择从库
        factory = pools.slave_session_factories[self._current_slave_index]
        self._current_slave_index = (self._current_slave_index + 1) % len(
            pools.slave_session_factories
        )

        async with factory() as session:
            try:
                yield session
            except Exception as e:
                logger.error(f"Slave session error: {e}")
                # 从库读取失败，降级到主库
                logger.warning("Slave read failed, fallback to master")
                async with self.get_master_session() as master_session:
                    yield master_session
            finally:
                await session.close()

    @asynccontextmanager
    async def get_session(self, read_only: bool = False) -> AsyncGenerator[AsyncSession, None]:
        """
        获取数据库会话 (自动读写分离)

        Args:
            read_only: 是否只读操作，True使用从库，False使用主库
        """
        if read_only:
            async with self.get_slave_session() as session:
                yield session
        else:
            async with self.get_master_session() as session:
                yield session

    async def health_check(self) -> dict:
        """健康检查（只查当前事件循环自己的池）"""
        result = {"master": False, "slaves": []}
        pools = self._pools_for_current_loop()

        # 检查主库
        try:
            async with pools.master_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                result["master"] = True
                logger.info("Master database health check: OK")
        except Exception as e:
            logger.error(f"Master database health check failed: {e}")
            result["master"] = False

        # 检查从库
        for idx, engine in enumerate(pools.slave_engines, 1):
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                    result["slaves"].append({"index": idx, "healthy": True})
                    logger.info(f"Slave database {idx} health check: OK")
            except Exception as e:
                logger.error(f"Slave database {idx} health check failed: {e}")
                result["slaves"].append({"index": idx, "healthy": False})

        return result

    async def get_pool_status(self) -> dict:
        """获取连接池状态（当前事件循环）"""
        status = {}
        pools = self._pools_for_current_loop()

        # 主库连接池状态
        if pools.master_engine:
            pool = pools.master_engine.pool
            status["master"] = {
                "size": pool.size(),
                "checked_in": pool.checkedin(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
                "total": pool.size() + pool.overflow(),
            }

        # 从库连接池状态
        status["slaves"] = []
        for idx, engine in enumerate(pools.slave_engines, 1):
            pool = engine.pool
            status["slaves"].append(
                {
                    "index": idx,
                    "size": pool.size(),
                    "checked_in": pool.checkedin(),
                    "checked_out": pool.checkedout(),
                    "overflow": pool.overflow(),
                    "total": pool.size() + pool.overflow(),
                }
            )

        return status

    async def dispose_current_loop(self) -> None:
        """释放**当前事件循环**独占的引擎（临时循环收尾用）。

        ``asyncio.run`` 关闭循环前必须调用：否则池里的 asyncpg 连接会在
        循环关闭后才被 GC，既刷 ``Event loop is closed``，也可能把连接留给
        下一个循环（正是本次事故的形态）。只动本循环的条目，主循环的池不受影响。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._pools_lock:
            pools = self._loop_pools.pop(loop, None)
        if pools is None:
            return

        if pools.master_engine is not None:
            await pools.master_engine.dispose()
        for engine in pools.slave_engines:
            await engine.dispose()
        with self._pools_lock:
            if not self._loop_pools:
                self._initialized = False
        logger.info("Database pools disposed for event loop %s", id(loop))

    async def close(self):
        """关闭当前事件循环的数据库连接（进程退出路径）。"""
        logger.info("Closing database connections...")
        await self.dispose_current_loop()
        logger.info("All database connections closed")


# 全局数据库管理器实例
_db_manager: DatabaseManager | None = None


def get_db_manager() -> DatabaseManager:
    """获取全局数据库管理器实例"""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


@asynccontextmanager
async def get_session(read_only: bool = False) -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话的便捷函数

    Usage:
        async with get_session() as session:
            # 写操作
            pass

        async with get_session(read_only=True) as session:
            # 读操作
            pass
    """
    db_manager = get_db_manager()
    async with db_manager.get_session(read_only=read_only) as session:
        yield session


async def init_database():
    """初始化数据库连接池"""
    db_manager = get_db_manager()
    await db_manager.initialize()


async def close_database():
    """关闭数据库连接池"""
    db_manager = get_db_manager()
    await db_manager.close()


async def dispose_current_loop_pools() -> None:
    """释放当前事件循环的 async DB 池（见 ``DatabaseManager.dispose_current_loop``）。"""
    db_manager = get_db_manager()
    await db_manager.dispose_current_loop()
