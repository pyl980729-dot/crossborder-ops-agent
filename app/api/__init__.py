"""HTTP 接口层。

routes.py 定义 REST 端点，deps.py 负责身份解析与权限校验；
两层都不做业务计算，只把请求转给 orchestrator。
"""
