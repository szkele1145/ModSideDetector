# 文档站发布说明（GitHub Pages）

本站点（`docs/` 目录下的静态 HTML）通过 **GitHub Pages 的「Deploy from a branch」** 发布，
**不使用 GitHub Actions**。

## 为什么不用 Actions

本仓库的令牌**没有 `workflow` scope**，添加 `.github/workflows/*.yml` 会被 GitHub 拒绝推送。
因此本项目**不创建任何 workflow 文件**，改用分支目录发布。

## 发布配置（手动一次性设置）

> 仓库 **Settings → Pages → Source** 选 **`Deploy from a branch`** →
> 分支 **`main`**、目录 **`/docs`** → Save

保存后等待 1–2 分钟，站点地址为：

```
https://szkele1145.github.io/ModSideDetector/
```

GitHub Pages 会直接从 `main` 分支的 `/docs` 目录读取 `index.html` 等文件，
所以**生成站点后必须把 `docs/` 下的 HTML 与 `docs/assets/` 一起提交**。

## 站点结构

| 文件 | 说明 |
|---|---|
| `docs/index.html` | 落地页（手写内容，由 `tools/build_site.py` 生成） |
| `docs/guide.html` | 使用指南（源：`README.md`） |
| `docs/strategy.html` | 判定策略（源：`docs/判定策略.md`） |
| `docs/handoff.html` | 技术交接文档（源：`HANDOFF.md`） |
| `docs/prompt.html` | 原始需求书（源：`PROMPT.md`） |
| `docs/assets/site.css` | 全部样式（深色科技风，无外部依赖） |
| `docs/assets/site.js` | 极简脚本：移动端导航、代码复制、回到顶部、目录高亮 |
| `docs/判定策略.md` | **文档源文件，必须保留**，不要因为它生成了 HTML 就删掉 |

## 本地生成与预览

```powershell
# 生成站点（在项目根执行）
python tools/build_site.py

# 只校验、不写文件
python tools/build_site.py --check

# 本地预览：直接用浏览器打开 docs\index.html 即可
# （站点为纯静态、无外部 CDN，file:// 协议下也能正常渲染）
```

生成器只写 HTML，**不会修改任何 Markdown 源文件**，重复运行结果一致（幂等）。

## 提交时注意

`.gitignore` 第 17 行原本是 `tools/`（整目录忽略，避免便携版 ffmpeg 之类的大文件入库），
这会把站点生成器 `tools/build_site.py` 一起忽略掉。因此该处已改成：

```gitignore
tools/*
# 例外：文档站生成器是纯文本脚本（几 KB），必须入库才能重新生成 docs/ 站点
!tools/build_site.py
```

> `tools/` 下的其它文件（如大体积便携工具）仍会被忽略。
> 如果你不希望 `tools/` 收录任何文件，删掉那行 `!tools/build_site.py` 即可，
> 但站点生成器将无法随仓库分发。

## 维护提示

- 改完 `README.md` / `HANDOFF.md` / `PROMPT.md` / `docs/判定策略.md` 后，**重跑一次生成器**再提交；
- 页面之间的跳转、返回首页、标题锚点都由生成器处理，不需要手工维护；
- 不要往站点里引入外部 CDN 资源（字体、CSS、JS），否则离线打开会失效。
