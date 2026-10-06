# ModSideDetector — 交接文档

> 本文档记录**已实测验证的技术结论**，接手者无需重复试错。
> 所有结论均来自对真实数据（151 个 NeoForge 1.21.1 模组）的实测。

---

## 一、项目是什么

一个**检测 Minecraft 模组「运行侧别」**的工具：判断每个 mod 属于

- **纯客户端**（只客户端装）
- **纯服务端**（只服务端装）
- **双端**（客户端+服务端都要装）

并提供 **GUI + 独立 exe**，最后与 **AutoSync**（另一个项目）联动。

**要解决的痛点是**：整合包作者手工分辨 mod 侧别极其耗时且容易错。装多了玩家白下载几百 MB，装少了直接崩游戏。

---

## 二、★ 数据源实测结论（核心资产）

### 2.1 MC 百科（mcmod.cn）—— **首选，质量最高**

**结论：可行，覆盖率 92%，语义最清晰。**

模组详情页有一个 **`运行环境`** 字段，直接给出侧别：

```
运行环境: 客户端需装, 服务端无效      ← 纯客户端
运行环境: 客户端需装, 服务端需装      ← 双端
运行环境: 客户端可选, 服务端无效
运行环境: 客户端需装, 服务端可选
运行环境: 客户端需装                  ← 只标了一半
```

**实测覆盖率：12 / 13 = 92%**

| 查询名 | classId | 运行环境 |
|---|---|---|
| 3D 皮肤层 | 883 | 客户端需装, 服务端无效 |
| 钠 | 332 | 客户端需装, 服务端需装 |
| 锂 | 36 | 客户端可选, 服务端无效 |
| Jade | 3482 | 客户端可选, 服务端可选 |
| 机械动力 | 2021 | 客户端需装, 服务端需装 |
| 创世神 | 628 | 客户端需装 |
| Xaero 的小地图 | 1701 | 客户端需装, 服务端可选 |
| 墓碑 | 527 | 客户端需装, 服务端需装 |
| 苹果皮 | 744 | 客户端需装, 服务端可选 |
| FramedBlocks | 5918 | 客户端需装, 服务端需装 |
| 铷 | 36 | 客户端可选, 服务端无效 |
| ModernFix | 8714 | 客户端可选, 服务端可选 |
| **架构师** | — | **搜索无结果**（mcmod 上叫 `Architectury`） |

**抓取方式（实测可用，无反爬，无需 JS）**

```python
UA = {"User-Agent": "Mozilla/5.0 ... Chrome/120 Safari/537.36"}

# 第一步：搜索拿 classId
#   GET https://www.mcmod.cn/s?key=<urlquote(名称)>&mold=1
#   正则： /class/(\d+)\.html      → 取第一个
#
# 第二步：抓详情页
#   GET https://www.mcmod.cn/class/<classId>.html
#   正则： 运行环境\s*[:：]\s*</?[^>]*>?\s*([^<\n]{2,40})
#   回退： 运行环境\s*[:：]\s*([^<\n]{2,40})
```

**⚠️ 已知坑**

1. **搜索匹配是最大难点**
   - `架构师` 搜不到（mcmod 条目叫 `Architectury`）
   - **建议**：用 jar 里 `neoforge.mods.toml` 的 `displayName`（通常含"中文名 (English Name)"）；搜不到时换英文名、modId、去括号重试
2. **新收录条目字段可能为空**
   - 例：`/class/31414.html`（Armor Hide，3 天前收录）**没有** `运行环境` 字段
   - 必须处理"字段缺失"，不能当成"无侧别信息"
3. **限流**：实测 **0.6 秒/请求**安全，**不要并发**
4. **⚠️ 许可证**：mcmod 内容为 **BY-NC-SA 3.0**
   - **禁止商业使用**
   - **转载需署名**
   - **不要把抓到的数据打包进分发的产物里**（可在本地缓存供自己使用）

---

### 2.2 Modrinth —— 现成的第二数据源

```
POST https://api.modrinth.com/v2/version_files
  body: {"hashes": ["<sha1>", ...], "algorithm": "sha1"}
  → 返回每个 hash 对应的 version 对象

GET https://api.modrinth.com/v2/projects?ids=["<id1>","<id2>"]
  → 每个 project 有 client_side / server_side
     取值： required / optional / unsupported
```

- **实测命中率：121 / 150 = 80.7%**（同一个 817 MB 整合包）
- 优点：**批量查询**（一次 100 个 hash）、有官方 API、无需抓页面
- 缺点：字段由**作者自己填**，经常不准（本项目就是因为它不准才另起这个工具）

---

### 2.3 Forge/NeoForge 的 `displayTest` 字段 —— **基本没用**

在 `META-INF/neoforge.mods.toml` 里，理论上能表示"客户端可以不装"。

**实测 144 个 jar 的分布**：

```
(未设置)                 134   ← 93%
IGNORE_ALL_VERSION         4
MATCH_VERSION              3
IGNORE_SERVER_VERSION      2
NONE                       1
```

**结论：93% 未设置，不可作为主要依据**（可作为弱信号）。

---

### 2.4 字节码扫描 —— **快，但准确率只有约 55%，不推荐做主依据**

**做法**：扫描 jar 内 class 文件的字节码，查找 `net/minecraft/client/` 与 `net/minecraft/server/` 字符串。

**速度**：151 个 jar **0.7 秒**跑完（只采样前 300 个 class 即可）

**实测分布**：

```
双端              102  (68%)
仅客户端引用        20  (13%)
没引用 MC 类        19  (13%)
仅服务端引用        10  (7%)
```

**但抽查后误判严重**：

| mod | 扫描判定 | 实际情况 | |
|---|---|---|---|
| `Axiom` | 没引用 MC 类 | **纯客户端**（建筑工具） | ❌ |
| `Flashback` | **仅服务端引用** | **纯客户端**（录像回放） | ❌ |
| `Xaero 小地图` | 双端 | **纯客户端** | ❌ |
| `Lambd 动态光源` | 没引用 MC 类 | **纯客户端** | ❌ |
| `worldedit` | 没引用 MC 类 | 双端 | ❌ |

**误判原因**
1. mod 常用 **mixin 字符串目标**（`"net.minecraft.client.xxx"` 只是字符串，扫到就是假阳性）
2. **编译期映射差异**（official / srg / mojmap）
3. **"引用了" ≠ "必需"**

**结论：只能当弱信号，权重给低。**

---

### 2.5 其它可考虑的信号

| 信号 | 说明 | 可靠性 |
|---|---|---|
| jar 内 `assets/` 存在 | 几乎所有 NeoForge mod 都有 | **极低**（区分度不足） |
| jar 内 `data/` 存在 | 说明有服务端数据包内容 | 中低 |
| jar 内 mixin 配置含 `client` | `xxx.mixins.json` 里 `client` 段 | 中 |
| `displayName` 含 "客户端"/"Client" | 名称关键词 | 低 |
| **人工复核** | 一次性标注，永久有效 | **100%** |

---

## 三、推荐的多源融合策略

**没有单一银弹。** 推荐 **投票 + 保守默认**：

```
        Modrinth side (80.7% 命中，作者填)
                    +
        mcmod 运行环境 (92% 命中，编辑者填)     ← 权重最高
                    +
        displayTest / 字节码特征 (弱信号)
                    ↓
              综合打分 & 冲突检测
                    ↓
    ┌───────────────┴───────────────┐
    │  两源一致        → 采信          │
    │  两源冲突        → 标记「需人工确认」│
    │  只有一源        → 采信但降置信度  │
    │  都没有          → 默认「双端」    │
    └───────────────────────────────┘
```

**保守默认原则**：**不确定的一律按「双端」**（发给客户端）——多下载一个 mod 顶多浪费带宽，少发一个会**直接崩游戏**。这条在 AutoSync 项目里已被血泪验证过。

---

## 四、与 AutoSync 的联动方案

AutoSync 是配套的**整合包自动同步系统**（仓库：`https://github.com/szkele1145/AutoSync`）。

### 4.1 联动点

AutoSync 有一个 `classify` 功能（`server/python/autosync/classify.py`），负责把 `client-dist/mods/` 里的 mod 分成纯客户端 / 双端 / 纯服务端，并把服务端需要的复制到服务端的 `mods/`。

**它的分类依据正是 Modrinth 的 side 字段——也就是本项目要改进的那个环节。**

### 4.2 推荐联动方式

**方式 A（推荐）：本项目输出 JSON，AutoSync 读取**

```
ModSideDetector 扫描 mods/ → 生成 side-report.json
                                    ↓
AutoSync 的 classify 优先读这个 JSON，没有再查 Modrinth
                                    ↓
                          分类准确率大幅提升
```

**side-report.json 建议格式**：

```json
{
  "generated": "2026-10-06T17:30:00+08:00",
  "tool": "ModSideDetector 1.0.0",
  "mods": [
    {
      "file": "sodium-neoforge-0.8.13+mc1.21.1.jar",
      "sha1": "…",
      "sha256": "…",
      "mod_id": "sodium",
      "display_name": "Sodium",
      "side": "client",          // client | server | both | unknown
      "confidence": "high",      // high | medium | low
      "sources": {
        "modrinth": { "client_side": "required", "server_side": "unsupported" },
        "mcmod":    { "class_id": 332, "run_env": "客户端需装, 服务端需装" },
        "heuristics": { "display_test": null, "has_client_mixin": true }
      },
      "notes": ""
    }
  ]
}
```

**方式 B：本项目直接调用 AutoSync 的分类逻辑**
复用 `classify.py`，避免两套逻辑不一致。

**方式 C（最轻）**：本项目只输出报表给人看，人工决定后手动改 AutoSync 的配置。

### 4.3 输出目录约定（与 AutoSync 对齐）

若提供「一键分类」功能，建议输出：

```
<输出目录>/
├── client-mods/     纯客户端（只发给玩家）
├── server-mods/     纯服务端（只放服务器）
└── both-mods/       双端（两边都要）
```

这三个目录名与 AutoSync 的 `classify` 语义一致。

---

## 五、技术选型建议

| 项 | 建议 | 理由 |
|---|---|---|
| 语言 | **Python 3.11+** | 与 AutoSync 同栈，复用 `deps.py` 里的 TOML 解析与 JiJ 递归扫描 |
| GUI | **CustomTkinter** | 基于 Tkinter（标准库），体积小、现代感强；比 PySide6 轻得多 |
| 打包 | **PyInstaller**（`--onefile --windowed`） | 出单文件 exe，双击即用 |
| 并发 | **ThreadPoolExecutor**（网络请求） + 主线程更新 UI | 抓 mcmod 要限流（0.6s/请求），用队列串行更稳 |
| 缓存 | 本地 JSON（`cache/mcmod-cid.json`、`cache/side-cache.json`） | 避免重复搜索；**缓存不要打包分发**（许可证） |
| 配置 | `config.json` | 与 AutoSync 风格一致 |

**⚠️ 打包注意**：PyInstaller 打的 exe 常被杀毒软件误报。缓解方式：
- 用 `--onedir` 替代 `--onefile`（误报率低）
- 或对 exe 做代码签名（需证书，成本高）
- **务必在 README 里说明这一点**

---

## 六、已验证的 Python 实现片段（可直接复用）

### 6.1 mcmod 查询

```python
import urllib.request, urllib.parse, re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"}

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def search_class_id(name):
    """返回 (classId, err)"""
    try:
        html = fetch("https://www.mcmod.cn/s?key=" + urllib.parse.quote(name) + "&mold=1")
    except Exception as e:
        return None, f"搜索失败: {e}"
    ids = re.findall(r"/class/(\d+)\.html", html)
    return (ids[0], "") if ids else (None, "无结果")

def fetch_run_env(class_id):
    """返回 (运行环境原文, err)"""
    try:
        html = fetch(f"https://www.mcmod.cn/class/{class_id}.html")
    except Exception as e:
        return None, str(e)
    m = (re.search(r"运行环境\s*[:：]\s*</?[^>]*>?\s*([^<\n]{2,40})", html)
         or re.search(r"运行环境\s*[:：]\s*([^<\n]{2,40})", html))
    return (m.group(1).strip(), "") if m else (None, "页面无该字段")
```

### 6.2 「运行环境」原文 → side 枚举

```python
def parse_run_env(text: str) -> str:
    """返回 client / server / both / unknown"""
    if not text:
        return "unknown"
    t = text.replace(" ", "")
    c_need = "客户端需装" in t
    c_opt  = "客户端可选" in t
    s_need = "服务端需装" in t
    s_opt  = "服务端可选" in t
    c_bad  = ("服务端无效" in t)          # 注意：这句描述的是服务端，不是客户端
    s_bad  = ("客户端无效" in t)

    has_client = c_need or c_opt
    has_server = s_need or s_opt

    if has_client and has_server:
        return "both"
    if has_client and (s_bad or not s_need):
        return "client"
    if has_server and (c_bad or not c_need):
        return "server"
    return "unknown"
```

> ⚠️ 上面这段**只是示例逻辑**，实测样本中的文案组合可能更多（如只有「客户端需装」半句），
> **接手者需要先用真实数据把文案穷举一遍再定稿**。

### 6.3 从 jar 读 displayName / modId

参考 AutoSync 仓库的 `server/python/autosync/deps.py`：
- 已实现 `[[mods]] #mandatory` 这种带行内注释的 TOML 解析
- 已实现 **JiJ 嵌套层递归**
- **直接复用，不要重写**

---

## 七、验收建议

1. **准确率基准**：准备 50 个**人工已确认**侧别的 mod 做测试集，要求综合准确率 **≥ 95%**
2. **保守性**：所有「无法判定」必须落在 `both` 或 `unknown`，**绝不能误判成 `server`**（会导致客户端缺 mod 崩游戏）
3. **性能**：150 个 mod 全量检测（含 mcmod 抓取）应在 **3 分钟内**完成
4. **断网可用**：mcmod 不可达时应能降级到 Modrinth / 纯启发式，并明确告知用户
5. **缓存生效**：第二次扫描不重复请求

---

## 八、相关资源

- **AutoSync 仓库**：https://github.com/szkele1145/AutoSync
  - `server/python/autosync/classify.py` — 现有分类逻辑
  - `server/python/autosync/deps.py` — TOML 解析 + JiJ 递归（可复用）
  - `docs/相比原版的改进.md` — 项目背景与技术取舍
- **mcmod**：https://www.mcmod.cn/
- **Modrinth API**：https://docs.modrinth.com/api/
- **CurseForge API**：`GET /v1/mods/{id}` 也返回 `clientSide` / `serverSide`（需 api key，可选第三数据源）

---

## 九、明确不要做的事

- ❌ **不要把 mcmod 抓来的数据打包进分发的产物**（BY-NC-SA 3.0 限制）
- ❌ **不要并发狂抓 mcmod**（限流 0.6s，尊重对方服务器）
- ❌ **不要用字节码扫描做主判据**（实测仅约 55% 准确）
- ❌ **不要把「不确定」判成 `server`**（代价是崩游戏）
- ❌ 不要重写 AutoSync 已有的 TOML/JiJ 解析（直接复用）
