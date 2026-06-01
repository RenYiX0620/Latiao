# TypeScript React Best Practices

## 约束
- 所有组件必须使用 TypeScript，禁止 any 类型
- useEffect 依赖数组不能有空，必须包含所有使用的外部变量
- 不使用 var，只用 const/let
- 优先使用 interface 而非 type（组件 props）
- 事件处理函数使用 handle 前缀（handleClick, handleChange）

## 退出标准
- tsc --noEmit 零错误通过
- 无 eslint error（warning 可接受）
- 组件文件不超过 300 行

## 示例
```tsx
// ✅ 正确
interface Props { title: string; onSave: (data: FormData) => void; }
const Modal: React.FC<Props> = ({ title, onSave }) => { ... };

// ❌ 错误
const Modal = (props: any) => { ... };
```
