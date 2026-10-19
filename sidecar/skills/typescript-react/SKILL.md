---
name: typescript-react
description: Use when 写或改 React/TS 代码。遵循类型安全、Hooks 与组件规范
---
# TypeScript React Best Practices

## 约束
- 所有组件必须使用 TypeScript，禁止 any 类型
- useEffect 依赖数组不能有空，必须包含所有使用的外部变量
- 使用 React.memo 包裹纯展示组件
