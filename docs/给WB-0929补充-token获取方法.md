# 回复 WorkBuddy（2026-09-29 补充）

不需要找授权链接，也不需要找我要 PAT。

**仓库文件 `.credentials/gh_tokens.env` 里已经有 classic PAT（`GH_TOKEN_CLASSIC`），有完整读写权限。**

你只需要在沙箱里读这个文件，把值设进环境变量：

```bash
# 在你的沙箱里执行
# 先 clone 仓库（如果还没有的话）
git clone https://github.com/livsstock/xgl.git
cd xgl

# 从仓库文件里读 token
export WB_GITHUB_TOKEN=$(grep GH_TOKEN_CLASSIC .credentials/gh_tokens.env | cut -d= -f2-)
```

设好之后直接 push 你的 commit `4d1443f` 就行了。

注意：这个 token 是 90 天有效的 classic PAT，对 livsstock/xgl 仓库有完整权限。

---
大海 2026-09-29
