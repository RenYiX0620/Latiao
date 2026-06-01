# Python FastAPI Best Practices

## 约束
- 使用 Pydantic 模型定义请求/响应体，而不是裸 dict
- 异步端点优先（async def），阻塞操作用 run_in_executor
- 异常用 HTTPException，不要返回裸错误字符串
- 敏感信息不硬编码，用环境变量

## 退出标准
- 所有端点有类型注解
- 无裸 except: 块（至少捕获 Exception）
- 关键端点有 try/except 包装

## 示例
```python
# ✅ 正确
class ItemCreate(BaseModel):
    name: str
    price: float

@app.post("/items")
async def create_item(item: ItemCreate) -> ItemResponse:
    ...

# ❌ 错误
@app.post("/items")
async def create_item(data: dict):
    ...
```
