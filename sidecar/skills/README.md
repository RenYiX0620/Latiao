# 🌶️ Latiao Skills

Latiao Agent 的社区技能仓库。每个技能是一个 SKILL.md 文件，Agent 按需加载。

## 如何使用

```bash
# 方法1: 直接复制
cp skills/web-search.md ~/latiao/sidecar/skills/

# 方法2: git clone
cd ~/latiao/sidecar/skills
git clone https://github.com/your-username/latiao-skills-community.git .
```

## 技能列表

| 技能 | 描述 | 作者 |
|------|------|------|
| [web-search](skills/web-search.md) | 免费联网搜索（DuckDuckGo + SearXNG） | community |
| [code-review](skills/code-review.md) | 代码审查与安全分析 | built-in |
| [git-workflow](skills/git-workflow.md) | Git 工作流规范 | built-in |

## 贡献技能

1. Fork 本仓库
2. 创建一个 `.md` 文件在 `skills/` 目录
3. 格式参考 [模板](TEMPLATE.md)
4. 提交 PR

### Skill 模板

```markdown
---
name: your-skill-name
description: 一句话描述
---
## 触发场景
...
## 规则
...
## 退出标准
...
```
