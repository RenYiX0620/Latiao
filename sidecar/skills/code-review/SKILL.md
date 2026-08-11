---
name: code-review
description: 代码审查清单 — 检查 import、硬编码、异常处理、函数长度
---
# Code Review Checklist

## 审查步骤（每次修改代码后自动执行）
1. 检查是否有未使用的 import
2. 检查是否有硬编码的敏感信息（密码、token、key）
3. 检查异常处理是否完整
4. 检查函数长度是否超过 50 行
