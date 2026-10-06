# ModSideDetector — 开发提示词

> **用法**：把本文件全文（或指向本文件的路径）交给 AI，让它实现这个项目。
> **前置阅读**：`HANDOFF.md`（内含所有已实测的技术结论、可复用代码片段、已知坑）
> **不要跳过 HANDOFF.md** —— 里面记录了数据源覆盖率、准确率实测值、以及必须避免的做法。

---

## 一、任务

开发 **ModSideDetector**：一个检测 Minecraft 模组「运行侧别」的 Windows 桌面工具。

**输入**：一个 `mods/` 文件夹（内含若干 `.jar`）
**输出**：每个 mod 的侧别判定（纯客户端 / 纯服务端 / 双端）+ 可视化界面 + 可导出报告

**要解决的问题**：整合包作者手工分辨 mod 侧别极耗时且易错。装多了玩家白下几百 MB，**装少了直接崩游戏**。

---

## 二、必须实现的功能

### 2.1 核心检测

对每个 mod 判定：`client` / `server` / `both` / `unknown`

**数据源优先级**（详见 `HANDOFF.md` 第二章，含覆盖率与准确率实测）：

1. **mcmod.cn 的「运行环境」字段** —— 覆盖率 **92%**，质量最高
2. **Modrinth 的 `client_side` / `server_side`** —— 命中率 **80.7%**，有批量 API
3. **启发式弱信号** —— `displayTest`、mixin 配置（**只作辅助，不可单独定案**）
4. **本地缓存** —— 避免重复请求

**多源融合规则**（详见 `HANDOFF.md` 第三章）：

```
两源一致       → 采信，置信度 high
两源冲突       → 标记「需人工确认」，置信度 low，界面高亮
只有一源       → 采信，置信度 medium
都没有         → both（保守默认），置信度 low
```

**⚠️ 铁律（必须遵守）**
- **绝不允许把「不确定」判成 `server`** —— 代价是客户端缺 mod 崩游戏
- 不确定时一律落到 `both` 或 `unknown`
- 必须提供**人工复核**入口（这是达到 100% 准确的唯一路径）

### 2.2 GUI

- **选文件夹**（记住上次路径）+ 拖拽支持
- **扫描进度**（进度条 + 当前正在查询的 mod 名 + 剩余时间估计）
- **结果表格**：文件名 / modId / displayName / **判定侧别** / 置信度 / 数据来源 / 备注
  - 支持按列排序、按侧别筛选
  - **冲突项与 unknown 项高亮**（这两个是用户最需要看的）
- **人工修正**：双击某行可手动改判定，改过的行标记「已人工确认」并**写进缓存**
- **导出**：
  - `side-report.json`（格式见 `HANDOFF.md` 4.2 节，供 AutoSync 联动）
  - `side-report.csv`（给人看 / Excel 打开）
  - `side-report.txt`（纯文本摘要）
- **一键分类**（可选功能）：按判定结果复制到 `client-mods/` / `server-mods/` / `both-mods/`
  - **默认是「复制」不是「移动」**
  - 若提供移动功能，必须有明确的二次确认

### 2.3 打包 exe

- **PyInstaller**：`--windowed`（无控制台窗口），优先 `--onedir`
- **单文件 exe 必须能双击运行**，不要求用户装 Python
- ⚠️ **README 里必须说明**：PyInstaller 打的 exe 常被杀毒误报，给出缓解建议
- 版本号与 `--version` 输出要正确

### 2.4 与 AutoSync 联动

**AutoSync** 是配套的整合包自动同步系统（https://github.com/szkele1145/AutoSync）。

联动方式（推荐 **方式 A**，详见 `HANDOFF.md` 第四章）：

```
ModSideDetector 扫描 → side-report.json
                              ↓
AutoSync 的 classify 优先读它，没有再查 Modrinth
                              ↓
                      分类准确率大幅提升
```

**要求**：
- `side-report.json` 格式必须**严格符合** `HANDOFF.md` 4.2 节的约定
- 输出目录名 `client-mods/` / `server-mods/` / `both-mods/` 必须与 AutoSync 的 `classify` 语义一致
- **可选加分**：提供 `--for-autosync <路径>` 参数，直接生成 AutoSync 能吃的文件

---

## 三、技术约束

| 项 | 要求 |
|---|---|
| 语言 | **Python 3.11+** |
| GUI | **CustomTkinter**（基于 Tkinter，无需额外运行时） |
| 打包 | **PyInstaller** |
| 网络 | **仅标准库 `urllib`**（避免 requests 增加打包体积与依赖问题） |
| 并发 | mcmod 抓取**必须限流 0.6 秒/请求、串行**（对方服务器友好）；Modrinth 可批量 |
| 缓存 | 本地 JSON，存 `cache/` |
| 配置 | `config.json`（与 AutoSync 风格一致） |
| 平台 | 首要目标 **Windows**（出 exe），但代码应保持跨平台可运行 |

**必须复用 AutoSync 的已有实现**（不要重写）：
- `server/python/autosync/deps.py` —— TOML 解析（含 `[[mods]] #mandatory` 行内注释）+ **JiJ 嵌套层递归**
- 直接复制过来或作为参考实现

---

## 四、必须遵守的约束（吃过亏的地方）

1. **⚠️ mcmod 数据不得打包进分发的产物**
   mcmod 内容为 **BY-NC-SA 3.0**：禁止商用、转载需署名。
   **本地缓存可以**，但**不要把它随 exe 一起分发**。

2. **⚠️ 不要并发狂抓 mcmod**
   实测 **0.6 秒/请求**安全。加上超时、重试上限（3 次）、失败降级。

3. **⚠️ 不要用字节码扫描做主判据**
   实测准确率仅约 **55%**（`Axiom`、`Flashback`、`Xaero 小地图` 等均被误判）。只能当弱信号。

4. **⚠️ `displayTest` 字段 93% 未填**
   可读但不可依赖。

5. **⚠️ 不要把「不确定」判成 `server`**
   宁可 `both`（多下载）也不能少发（崩游戏）。

6. **⚠️ 搜索匹配是最大难点**
   `架构师` 在 mcmod 上叫 `Architectury`。必须多策略重试：
   `displayName` 原文 → 去括号 → 括号内英文名 → modId → 逐个词。

---

## 五、交付物

```
ModSideDetector/
├── README.md              项目说明（含 exe 使用说明与杀毒误报提示）
├── HANDOFF.md             技术交接文档（已存在，不要删）
├── PROMPT.md              本文件
├── LICENSE                MIT
├── requirements.txt
├── config.example.json
├── src/
│   ├── main.py            GUI 入口
│   ├── detector.py        核心检测编排（多源融合）
│   ├── sources/
│   │   ├── mcmod.py       mcmod 抓取（限流 + 缓存）
│   │   ├── modrinth.py    Modrinth 批量 API
│   │   └── heuristics.py  jar 特征（displayTest / mixin）
│   ├── jarinfo.py         jar 元数据解析（复用 AutoSync 的 deps.py）
│   ├── report.py          导出 JSON / CSV / TXT
│   ├── cache.py           缓存管理
│   └── ui/                CustomTkinter 界面
├── tests/                 单元测试（含 50 个已知侧别的样例做准确率基准）
└── build.ps1              PyInstaller 打包脚本
```

**并产出**：`dist/ModSideDetector.exe`（双击可用）

---

## 六、验收标准

1. **准确率**：在人工确认的 50 个 mod 测试集上，**综合准确率 ≥ 95%**
2. **保守性**：所有无法判定的项必须落在 `both` / `unknown`，**误判成 `server` 的数量必须为 0**
3. **性能**：150 个 mod 全量检测（含 mcmod 抓取）**3 分钟内**完成
4. **断网可用**：mcmod 不可达时自动降级到 Modrinth / 启发式，并**明确告知用户**
5. **缓存生效**：第二次扫描不重复网络请求
6. **exe 可用**：`dist/ModSideDetector.exe` 双击能打开 GUI 并完成一次完整检测
7. **联动可用**：导出的 `side-report.json` 能被 AutoSync 的 `classify` 正确读取

---

## 七、开发建议顺序

1. 先做 **CLI 版**（`jarinfo` + `mcmod` + `modrinth` + 融合逻辑），把准确率跑出来
2. 准确率达标后再套 **GUI**
3. GUI 完成后**最后**做 **PyInstaller 打包**（打包问题最容易耗时，放最后）
4. 全程用真实数据验证 —— **不要用编造的 mod 名测试**

---

## 八、参考

- **详细技术结论与可复用代码**：见 `HANDOFF.md`（**必读**）
- **AutoSync 仓库**：https://github.com/szkele1145/AutoSync
- **mcmod**：https://www.mcmod.cn/
- **Modrinth API 文档**：https://docs.modrinth.com/api/

---

## 九、汇报要求

完成后请说明：
- 交付了哪些文件、exe 在哪
- **准确率实测结果**（测试集大小、命中数、误判清单）
- 用了哪些数据源、各自命中率
- 与 AutoSync 联动的实际验证方式
- **遗留问题与已知限制**
