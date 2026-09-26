"""测试包。

加 __init__.py 的目的：让 `tests.conftest` 成为**确定的**可导入路径（`from tests.conftest
import SYNTH_INDICATORS`）。否则 pytest 以 rootdir 方式把 conftest 载成顶层模块 `conftest`，
而测试文件里的 `import tests.conftest` 会**再加载一份**，同一份夹具数据出现两个模块实例，
调试时极易看花眼。
"""
