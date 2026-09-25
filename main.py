"""示例 FastAPI 服务入口：挂载 API 网关准入层。

业务路由（路由路径即配额与权限的作用域 key）：
- GET  /healthz        健康检查（公开，不经准入）
- GET  /v1/echo        读接口（scope: echo:read；不同版本语义一致）
- POST /v1/orders      写接口（scope: orders:write；必须幂等键；有副作用计数）
- POST /__test/reset   测试辅助：重置配额/幂等/审计内存状态（公开）
- GET  /__test/state   测试辅助：查看副作用计数与审计事件（公开）

受保护路由需要 X-Api-Key；版本通过 X-Api-Version 选择。
"""

from __future__ import annotations

import logging

from gateway.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
