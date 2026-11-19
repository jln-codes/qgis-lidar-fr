"""Run the current asynchronous QGIS console script, retaining iface and task references."""
from pathlib import Path
import inspect

_entrypoint = Path(inspect.currentframe().f_code.co_filename).resolve().with_name("lidar_ign.py")
exec(compile(_entrypoint.read_text(encoding="utf-8"), str(_entrypoint), "exec"), globals())
