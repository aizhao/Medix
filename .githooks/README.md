# Git hooks

`prepare-commit-msg` 会在没有显式提交信息时调用本机 Codex CLI，根据暂存区 diff 生成中文候选提交信息。Git 随后仍会打开默认编辑器，提交前可以人工确认和修改。

安装：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/prepare-commit-msg
```

使用 `git commit -m "已有信息"` 时不会覆盖用户提供的提交信息；Codex 不可用或调用失败时，钩子会跳过自动生成，不阻塞提交。
