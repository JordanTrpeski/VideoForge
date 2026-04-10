"""
logger.py
=========
Shared logging setup used by every VideoForge module.

Input:  module_name string
Output: configured logging.Logger instance
Logs:   logs/<module_name>.log, logs/main.log, logs/errors.log

Dependencies:
    - logging (stdlib)
    - os (stdlib)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import logging
import os


def setup_logger(module_name: str) -> logging.Logger:
    """
    Set up dual logging: module-specific file + combined main.log + errors.log.

    Args:
        module_name (str): Name of the calling module e.g. 'script_engine'.
                           Used as the logger name and log filename.

    Returns:
        logging.Logger: Fully configured logger instance with four handlers:
                        module file, main.log, errors.log, and console.
    """
    os.makedirs('logs', exist_ok=True)

    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers if function called multiple times
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 1. Module-specific log file
    module_handler = logging.FileHandler(f'logs/{module_name}.log')
    module_handler.setLevel(logging.DEBUG)
    module_handler.setFormatter(formatter)

    # 2. Combined main.log
    main_handler = logging.FileHandler('logs/main.log')
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(formatter)

    # 3. Errors-only log
    error_handler = logging.FileHandler('logs/errors.log')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    # 4. Console output — use UTF-8 safe stream on Windows
    import io
    import sys as _sys
    _stream = _sys.stdout
    if hasattr(_stream, 'buffer'):
        _stream = io.TextIOWrapper(_stream.buffer, encoding='utf-8', errors='replace')
    console_handler = logging.StreamHandler(_stream)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(module_handler)
    logger.addHandler(main_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)

    return logger
