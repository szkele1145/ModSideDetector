# ModSideDetector

检测 Minecraft 模组**运行侧别**的桌面工具 —— 判断每个 mod 属于 **纯客户端** / **纯服务端** / **双端**，并导出报告供 [AutoSync](https://github.com/szkele1145/AutoSync) 直接消费。

```
装多了 → 玩家白下载几百 MB
装少了 → 直接崩游戏（Missing or unsupported mandatory dependencies）
```

---

## 它解决什么问题

整合包作者手工分辨 mod 侧别极耗时且容易错。现有方案（Modrinth 的 `client_side` / `server_side`）由 **mod 作者自己填**，经常不准 —— 这正是本项目要改进的环节。

ModSideDetector 的做法是**多源交叉**：MC 百科（mcmod.cn）的「运行环境」字段 + Modrinth 的 side 字段 + jar 内静态特征，冲突时请人来定，判不了时**保守放双端**。

---

## 快速开始

### 图形界面（推荐）

```powershell
python -m src.main                        # 打开 GUI
python -m src.main --mods-dir "D:\mc\mods"  # 打开并立即开始扫描
```

或者直接双击 `dist\ModSideDetector\ModSideDetector.exe`（免装 Python，见下方「打包」）。

界面上可以：选/拖入 `mods` 文件夹 → 看进度 → 结果表格按列排序、按侧别筛选 → **双击某行人工改判**（结论写入缓存，下次扫描直接沿用）→ 导出报告 → 一键分类。

> **拖拽是软依赖**：装了 `tkinterdnd2` 就能把文件夹直接拖进窗口，没装（或 `tkdnd`
> 二进制加载失败）时界面只是显示一行提示并禁用拖拽，其余功能完全不受影响。
> 界面底部也会说明当前是否启用。

### 命令行

```powershell
# 扫描并导出 side-report.json / .csv / .txt
python -m src scan "D:\mc\mods"

# 只跑 Modrinth（跳过 mcmod 抓取，快很多）
python -m src scan "D:\mc\mods" --no-mcmod

# 额外写一份给 AutoSync 读的文件
python -m src scan "D:\mc\mods" --for-autosync "D:\AutoSync\data"

# 人工确认某个 mod（写进缓存，永久生效）
python -m src review "D:\mc\mods" --name sodium.jar --side client --note "服主确认"

# 按报告一键分类（默认只复制，不移动原文件）
python -m src classify "D:\mc\mods" --out "D:\mc\server"

# 查看缓存统计 / 清理缓存
python -m src cache --show
python -m src cache --clear
```

> ⚠️ **全局参数要放在子命令前面**：`python -m src --verbose scan ...` 是对的，
> `python -m src scan ... --verbose` 会报 `unrecognized arguments`（argparse 的行为）。

`scan` 的常用开关：

| 开关 | 作用 |
|---|---|
| `--out-dir <目录>` | 报告输出目录（默认与 mods 目录相同） |
| `--json-only` | 只写 JSON，不写 CSV/TXT |
| `--for-autosync <目录>` | 额外写一份 AutoSync 可读的 `side-report.json` |
| `--limit N` | 只扫描前 N 个 jar（冒烟测试用） |
| `--no-mcmod` / `--no-modrinth` / `--no-heuristics` | 关闭对应数据源 |
| `--offline` | 完全离线（等价于关掉 mcmod + Modrinth） |
| `--refresh` | 忽略缓存重新查询 |
| `--min-interval 1.0` | 调大 mcmod 抓取间隔（默认 0.6 秒） |

---

## 判定结论怎么读

| 判定 | 含义 | 分发含义 |
|---|---|---|
| `纯客户端` | 只客户端装 | 只发给玩家 |
| `纯服务端` | 只服务端装 | 只放服务器 |
| `双端` | 两边都要 | 两边都装 |
| `不确定` | 判不了 | 按配置处理（默认当双端） |

| 置信度 | 含义 |
|---|---|
| 高 | 两源一致，或某源给出**明确依据**（如 `client_side=unsupported`） |
| 中 | 只有一个数据源有数据 |
| 低 | 两源冲突 / 无数据源 / 弱信号兜底 |

**界面上要重点看两类行**：`冲突`（两源结论相反）与 `需人工确认`。其余行扫一眼即可。

冲突时工具**不做「谁更权威」的猜测**，直接判双端并请你人工确认 —— 猜错的代价是崩游戏。唯一例外：mcmod 明写「服务端无效 / 客户端无效」（明确依据）而 Modrinth 只有弱依据时，采信 mcmod，但置信度压到低并要求复核。

详细的规则、权重与实测坑见 **[docs/判定策略.md](docs/判定策略.md)**。

---

## 数据源与实测覆盖率

在**真实数据**（151 个 NeoForge 1.21.1 模组，817 MB）上的实测：

| 数据源 | 覆盖率 | 说明 |
|---|---|---|
| **mcmod「运行环境」** | 见下方准确率章节 | 语义最清晰，**质量最高**，需限流抓取 |
| **Modrinth** | **120/151 = 79.5%** | 官方批量 API，快；但字段由作者自填，常不准 |
| jar 元数据（`clientSideOnly` / Fabric `environment`） | 约 6% | 作者显式声明，可信但覆盖率低 |
| mixin / `data/` 等启发式 | 100% | **只作提示，绝不单独定案** |
| 人工复核 | 100% | 一次性确认，写进缓存永久生效 |

**刻意不做的事**：不扫字节码。`HANDOFF.md` 实测字节码扫描准确率仅约 55%（`Axiom`、`Flashback`、`Xaero 小地图` 全被误判）。

---

## 与 AutoSync 联动

```
ModSideDetector scan → side-report.json → AutoSync 的 classify 优先读它
                                       → 读不到才回退查 Modrinth
```

`side-report.json` 的格式严格按 `HANDOFF.md` 4.2 节的约定（`generated` / `tool` / `mods[]`，每条含 `file` / `sha1` / `sha256` / `mod_id` / `display_name` / `side` / `confidence` / `sources` / `notes`）。

生成方式：

```powershell
python -m src scan "D:\mc\mods" --for-autosync "D:\AutoSync\data"
```

一键分类的输出目录名也与 AutoSync 的 `classify` 语义一致：

```
<输出目录>/
├── client-mods/     纯客户端（只发给玩家）
├── server-mods/     纯服务端（只放服务器）
└── both-mods/       双端 + 不确定（两边都要）
```

> **一键分类默认是「复制」不是「移动」**，且目标同名文件内容不同时**绝不覆盖**（只记冲突）。
> 源文件永远不动 —— 删错了没法恢复，复制错了只是浪费磁盘。

---

## 打包自己的 exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
# 产物：dist\ModSideDetector\ModSideDetector.exe
```

脚本采用 **`--onedir`**（不是 `--onefile`）+ `--windowed`，并排除 matplotlib/numpy/PyQt 等无关大件。

> ⚠️ **杀毒软件误报**：PyInstaller 打包的 exe **经常被杀毒软件误报**，这不是本程序的问题，是打包器的通用现象。缓解办法：
> 1. 用 `--onedir`（本脚本的默认，误报率明显低于 `--onefile`）；
> 2. 把 `dist\ModSideDetector\` 整个目录加入杀软白名单（**不要只加 exe**，它依赖同目录的 `_internal\`）；
> 3. 从源码自己跑（`python -m src.main`），完全绕开打包器；
> 4. 对 exe 做代码签名（需购买证书，成本较高）。

### 打包产物里没有什么

- **不含 `cache/`** —— 里面是 mcmod.cn 抓来的内容，许可为 [BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/)，**禁止随产物分发**；
- **不含 `config.json`** —— 里面有本机路径、代理等私人配置；程序首次运行会在 exe 所在目录自动生成。

---

## 配置文件

配置优先级：`--config <路径>` > `<程序目录>/config.json` > 默认值。

首次运行会自动生成 `config.json`（GUI 会记住上次扫描的目录）。字段说明见 `config.example.json`，常用项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `mods_dir` | `""` | 上次扫描的目录（自动记忆） |
| `output_dir` | `""` | 报告输出目录，空 = mods 目录 |
| `cache_dir` | `""` | 缓存目录，空 = `<程序目录>/cache` |
| `use_mcmod` / `use_modrinth` / `use_heuristics` | `true` | 数据源开关 |
| `mcmod_min_interval` | `0.6` | mcmod 抓取间隔（秒），**调小会被对方限流** |
| `cache_ttl_hours` | `720` | 缓存有效期，`<=0` 表示永不过期 |
| `unknown_as` | `"both"` | 判不了时怎么办。**只接受 `both` / `unknown`**，写成 `server` 会被强制纠正 |
| `proxy` | `""` | 形如 `http://127.0.0.1:7890` |

---

## 缓存与许可证（红线）

- 缓存写在 `cache/side-cache.json`，包含 jar 哈希、Modrinth 查询结果、mcmod「运行环境」原文、以及人工复核结论；
- **mcmod 的内容是 BY-NC-SA 3.0：禁止商业使用、转载需署名**。本地缓存自己用没问题，**不要把缓存打包进分发的产物**（`.gitignore` 与 `build.ps1` 都已排除）；
- mcmod 抓取**串行限流 0.6 秒/请求**，不要并发 —— 尊重对方服务器，这也是 `HANDOFF.md` 明确列出的红线。

---

## 目录结构

```
ModSideDetector/
├── README.md              本文件
├── PROMPT.md              原始需求书
├── HANDOFF.md             技术交接文档（实测结论 + 红线，勿删）
├── docs/
│   └── 判定策略.md         判定规则、置信度语义、开发阶段新发现的坑
├── config.example.json    配置示例
├── requirements.txt
├── build.ps1              PyInstaller 打包脚本
├── src/
│   ├── main.py            GUI 入口
│   ├── cli.py             命令行入口（python -m src）
│   ├── detector.py        核心编排 + 多源融合
│   ├── jarinfo.py         jar 元数据解析（TOML / Fabric / JiJ 递归 / 哈希）
│   ├── cache.py           本地缓存（含人工复核结论）
│   ├── report.py          导出 JSON / CSV / TXT + 一键分类
│   ├── net.py             urllib 封装（重试 / 代理 / 限流）
│   ├── verdict.py         判定结果枚举
│   ├── config.py, paths.py
│   ├── sources/
│   │   ├── mcmod.py       MC 百科（限流 + 多策略搜索 + 标题校验）
│   │   ├── modrinth.py    Modrinth 批量 API
│   │   └── heuristics.py  jar 内弱信号
│   └── ui/app.py          CustomTkinter 界面
└── tests/                 单元测试 + 人工确认基准集
```

---

## 开发与测试

```powershell
python -m pip install -r requirements.txt

# 单元测试（不需要网络）
python -m unittest discover -s tests -v

# 准确率基准测试（需要先生成报告）
python -m src scan "D:\mc\mods" --out-dir out
python -m unittest tests.test_accuracy -v
```

要求 **Python 3.11+**。运行期只用标准库 + `customtkinter`，网络层是标准库 `urllib`（不依赖 `requests`）。

---

## 已知限制

- **Modrinth 字段由作者自填**，可能整条都是错的；工具会在两源冲突时高亮，但**最终仍需要人工过一遍**冲突项与「需人工确认」项 —— 这是达到 100% 准确的唯一路径；
- **某些 mod 在任何数据源上都查不到**（例如作者没发布到 Modrinth、mcmod 也没收录），此时只能保守判双端；
- **mcmod 新收录条目可能没有「运行环境」字段**：这是「字段缺失」，不等于「无侧别信息」，工具会退回其它数据源；
- 首次全量扫描需要抓 mcmod，**耗时与 mod 数量成正比**（限流 0.6 秒/请求，151 个 mod 约 2–3 分钟）；第二次扫描走缓存，几秒完成；
- `--windowed` 打包出的 exe 从命令行调用时，输出走的是父控制台而非管道，脚本里用管道捕获可能拿不到文字。

---

## 红线（摘自 HANDOFF.md，务必遵守）

- ❌ 不要把 mcmod 抓来的数据打包进分发的产物（BY-NC-SA 3.0）
- ❌ 不要并发狂抓 mcmod（限流 0.6 秒/请求）
- ❌ 不要用字节码扫描做主判据（实测准确率仅约 55%）
- ❌ 不要把「不确定」判成 `server`（代价是客户端缺 mod 崩游戏）
- ❌ 不要重写 AutoSync 已有的 TOML / JiJ 解析

## 许可证

本项目管理器代码采用 MIT（见 [LICENSE](LICENSE)）。
**注意**：mcmod.cn 的内容为 BY-NC-SA 3.0，与本项目的 MIT 许可无关，不要混淆。
