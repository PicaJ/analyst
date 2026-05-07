"""
日志配置 — 基于 loguru

功能:
  - 控制台: 彩色格式, INFO 级别
  - 全量文件: 按日轮转, DEBUG 级别
  - 错误文件: WARNING+ 级别
  - 磁盘空间限制: 达到上限时自动清理最旧的日志
"""

import shutil
import sys
from pathlib import Path

from loguru import logger


def _cleanup_logs(log_dir: Path, max_size_gb: float = 8.0, cleanup_size_gb: float = 5.0):
    """检查日志目录总大小，超过上限时删除最旧的日志文件

    Args:
        log_dir: 日志目录
        max_size_gb: 触发清理的大小上限 (GB)
        cleanup_size_gb: 清理时删除多少 (GB)，从最旧的文件开始删
    """
    if not log_dir.exists():
        return

    max_bytes = int(max_size_gb * 1024**3)
    cleanup_bytes = int(cleanup_size_gb * 1024**3)

    log_files = sorted(
        log_dir.glob("*.log*"),
        key=lambda f: f.stat().st_mtime,
    )

    total_size = sum(f.stat().st_size for f in log_files)
    if total_size < max_bytes:
        return

    logger.warning(
        "日志目录 {} 达到 {:.2f}GB (上限 {:.1f}GB)，开始清理最旧的 {:.1f}GB",
        log_dir, total_size / 1024**3, max_size_gb, cleanup_size_gb,
    )

    freed = 0
    for f in log_files:
        if freed >= cleanup_bytes:
            break
        size = f.stat().st_size
        f.unlink()
        freed += size
        logger.info("已删除旧日志: {} ({:.1f}MB)", f.name, size / 1024**2)

    logger.info("日志清理完成，释放 {:.2f}GB", freed / 1024**3)


def setup_logging(
    level: str = "INFO",
    log_dir: str | None = None,
    retention: str = "7 days",
    error_retention: str = "14 days",
    max_size_gb: float = 8.0,
    cleanup_size_gb: float = 5.0,
):
    """配置日志输出

    - 控制台: 彩色格式, INFO 级别
    - 文件: 按日轮转, DEBUG 级别
    - 错误文件: WARNING+ 级别
    - 启动时检查磁盘空间，超限自动清理
    """
    logger.remove()

    # 控制台
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level:<7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # 启动时检查磁盘空间
        _cleanup_logs(log_path, max_size_gb, cleanup_size_gb)

        # 全量日志
        logger.add(
            str(log_path / "analyst_{time:YYYY-MM-DD}.log"),
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}",
            rotation="00:00",
            retention=retention,
            compression="gz",
            encoding="utf-8",
        )

        # 错误日志
        logger.add(
            str(log_path / "analyst_error_{time:YYYY-MM-DD}.log"),
            level="WARNING",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}",
            rotation="00:00",
            retention=error_retention,
            compression="gz",
            encoding="utf-8",
        )
