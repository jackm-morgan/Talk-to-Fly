import logging
from datetime import datetime
import os
import re

                                                 
STATUS = 25
VERBOSE = 15
TRACE = 5

logging.addLevelName(STATUS, "STATUS")
logging.addLevelName(VERBOSE, "VERBOSE")
logging.addLevelName(TRACE, "TRACE")

def status(self, msg, *args, **kwargs):
    if self.isEnabledFor(STATUS):
        self._log(STATUS, msg, args, **kwargs)

def verbose(self, msg, *args, **kwargs):
    if self.isEnabledFor(VERBOSE):
        self._log(VERBOSE, msg, args, **kwargs)

def trace(self, msg, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, msg, args, **kwargs)

logging.Logger.status = status
logging.Logger.verbose = verbose
logging.Logger.trace = trace

                                                  
os.makedirs("logs", exist_ok=True)
existing_logs = os.listdir("logs")
pattern = re.compile(r"(\d+)_\d{8}_\d{6}\.log")
counters = [int(pattern.match(f).group(1)) for f in existing_logs if pattern.match(f)]
log_counter = max(counters) + 1 if counters else 1
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"logs/{log_counter}_{timestamp}.log"

def get_log_filename():
    return log_filename
                                                                 
class AlignFormatter(logging.Formatter):
    """Formatter that pads levelname for aligned output."""
    def format(self, record):
        record.levelname = record.levelname.ljust(8)                  
        return super().format(record)

formatter = AlignFormatter("%(asctime)s | %(levelname)s | %(message)s",
                           "%Y-%m-%d %H:%M:%S")

                                              
logger = logging.getLogger("uav_logger")
logger.setLevel(TRACE)
logger.propagate = False

                              
file_handler = logging.FileHandler(log_filename)
file_handler.setLevel(TRACE)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

                                                  
class LevelFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level
    def filter(self, record):
        return record.levelno == self.level

                                             
console_status_handler = logging.StreamHandler()
console_status_handler.setLevel(STATUS)
console_status_handler.setFormatter(formatter)
console_status_handler.addFilter(LevelFilter(STATUS))
logger.addHandler(console_status_handler)

                                               
console_verbose_handler = logging.StreamHandler()
console_verbose_handler.setLevel(VERBOSE)
console_verbose_handler.setFormatter(formatter)
console_verbose_handler.addFilter(LevelFilter(VERBOSE))

VERBOSE_MODE = False

def set_verbose(enabled: bool):
    global VERBOSE_MODE
    VERBOSE_MODE = enabled
    if enabled and console_verbose_handler not in logger.handlers:
        logger.addHandler(console_verbose_handler)
    elif not enabled and console_verbose_handler in logger.handlers:
        logger.removeHandler(console_verbose_handler)

                                                         
def log_status(msg: str):
    logger.status(msg)

def log_verbose(msg: str):
    logger.verbose(msg)

def log_trace(msg: str):
    logger.trace(msg)
