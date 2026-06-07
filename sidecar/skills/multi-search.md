# 🔍 Multi Search Engine — Latiao 联网搜索技能

让 Latiao 具备实时联网搜索能力。聚合多个免费搜索引擎结果，无需 API Key。

## 触发场景

- 用户问"搜索 XXX"、"查一下 XXX"、"帮我找 XXX"
- 用户询问实时信息（新闻、天气、股票等）
- 用户要求查找最新资料

## 使用方法

Agent 检测到搜索意图时，自动调用本技能。

### 搜索接口

```
调用格式: web_search("你的查询")
返回: 格式化的搜索结果（标题 + 链接 + 摘要）
```

## 数据源

- DuckDuckGo (免费，无 API Key)
- SearXNG (自托管元搜索，可选)
- Bing (需 API Key，可选)

## 示例

用户: "搜索今天的A股走势"
Agent: 调用 web_search("今天A股大盘走势 上证指数") → 返回实时结果

## 配置

```bash
# 可选：配置 SearXNG 自托管实例
export SEARXNG_URL="http://localhost:8888"

# 可选：配置 Bing API
export BING_API_KEY="your-key"
```

## 已知限制

- DuckDuckGo 对中文搜索优化不如百度
- 搜索结果可能受网络环境影响
- 建议配合 Tavily 使用以获得更好的中文搜索效果
