"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import lineage_ops, resource

# 固定路径接口要排在通用 /{module}/{entry_id} 之前注册
ROUTERS = [lineage_ops, resource]
