---
name: python-fastapi
description: Python FastAPI 最佳实践 — Pydantic、异步、错误处理
---
# Python FastAPI Best Practices

## 约束
- 使用 Pydantic 模型定义请求/响应体，而不是裸 dict
- 异步端点优先（async def），阻塞操作用 run_in_executor
- 所有外部 API 调用需要超时和重试
