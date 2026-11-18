"""离线评测：评测集生成、执行准确率、回归门禁。"""

from .cases import build_cases, load_cases, write_cases
from .metrics import result_equal, summarize

__all__ = ["build_cases", "load_cases", "write_cases", "result_equal", "summarize"]
