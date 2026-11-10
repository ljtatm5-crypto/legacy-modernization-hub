# 上传到 GitHub

这个包已经按仓库结构整理好，`.gitignore` 会挡住产物、密钥和第三方克隆。
下面三条命令就能推上去。

---

## 0. 先解压

解压后是一个目录 `legacy-modernization-hub/`，这就是仓库根目录。

```bash
cd legacy-modernization-hub
```

---

## 1. 初始化并提交

```bash
git init -b main
git add .
git commit -m "feat: 遗留系统确定性重构与迁移中枢（初始版本）"
```

提交完可以确认一下**没有**误传这些东西：

```bash
git ls-files | grep -E "artifacts/|model\.json|_dataset/|dist/" || echo "干净"
```

应当输出「干净」。如果列出了文件，说明 `.gitignore` 被覆盖了，先检查再 push。

---

## 2. 关联远程仓库

先在 GitHub 网页上建一个**空仓库**（不要勾选 README / .gitignore / License，避免冲突），然后：

```bash
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

---

## 3. 仓库设置建议

| 项 | 建议 |
|---|---|
| About → Description | 遗留系统确定性重构与迁移中枢 · 行为等价性闸门 · 可配置 DAG |
| About → Topics | `java` `legacy-modernization` `ast` `differential-testing` `llm` `code-generation` `mcp` |
| About → Website | 有部署地址就填，没有留空 |
| Settings → Actions | 保持默认开启 |

---

## 包内包含什么

```
README.md               项目说明（含架构、命令、效率口径）
LICENSE                 Apache-2.0
GITHUB.md               本文件
docs/VIDEO-SCRIPT.md    演示视频脚本（含点击顺序与时间轴）
docs/ARCHITECTURE.md    模块划分与数据流
screenshots/            README 用的界面截图
.github/workflows/ci.yml 持续集成：语法检查 + 巡检器编译 + MCP 自检
hub/                    引擎、后端、MCP、样例工程、模板、工作流定义
start.bat / start.sh    一键启动
package.py              打包脚本（可再生成分发 zip）
```

**不含**（`.gitignore` 已排除）：

- `hub/artifacts/` —— 运行产物，跑一次就有了
- `hub/model.json` —— API 密钥
- `hub/runs/` —— 历史轮次归档
- `_dataset/` —— Apache Commons Lang 的克隆，需要用 `cli.py dataset` 时按 README 自行 clone
- `dist/` —— 打出来的分发包

---

## 关于 CI

`.github/workflows/ci.yml` 会在每次 push 时跑：

1. Python 语法检查（`compileall`）
2. Node 语法检查（`node --check`）
3. 编译 AST 巡检器（`javac`）
4. 生成适配器自检（`cli.py probe`）
5. MCP 服务自检（9 项）

**不需要任何密钥**：没有 `hub/model.json` 时会自动回退规则适配器。

如果首次运行失败，最可能的原因是 JDK 版本。把 workflow 里的
`java-version: '21'` 改成 `'17'`（或反过来）再试。

---

## 常见问题

**Q：push 被拒，提示文件过大？**
正常不会。仓库里没有大于 1 MB 的单个文件（截图约 100 KB）。如果误加了 `_dataset/`，用
`git rm -r --cached _dataset` 后再提交。

**Q：想换许可证？**
把 `LICENSE` 换成你要的文本，并在 README 底部同步改一行。默认给的是 Apache-2.0，
与项目内置的 Apache Commons Lang 数据集兼容。

**Q：跑完演示后 `git status` 里 `ModernSettleService.java` 变成已修改？**
正常。那是 `/migrate-module` **生成出来的目标实现**，每次运行都会重写。
仓库里保留它，是为了让人 clone 下来就能读到"迁移后的代码长什么样"，
也保证干净仓库下 `run_build` 能通过。提交与否都行，建议不提交。

**Q：`hub/model.json` 被我误提交了怎么办？**
立即去模型厂商控制台吊销该 key，然后：

```bash
git rm --cached hub/model.json
git commit -m "chore: 移除误提交的密钥"
```

历史记录里仍然存在，最稳妥是吊销重发。
