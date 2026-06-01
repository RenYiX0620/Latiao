# Git Workflow

## 约束
- 不在 main/master 分支上直接修改，先创建 feature 分支
- commit message 格式：type(scope): description
  - type: feat/fix/refactor/docs/chore
  - scope: 受影响的模块名
- 推送前先 git pull --rebase

## 退出标准
- git status 干净（所有变更已提交）
- commit message 符合格式
