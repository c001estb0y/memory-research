# DPAR 项目 — Hughes Li 与 Ziyad 共同遇到的三个核心问题

> 数据来源：`dpar-hughesli.db`（599 条 session_summaries，1624 条 observations）  
> `dpar-ziyad.db`（1441 条 session_summaries，8680 条 observations）

---

## 问题一：NeverCook 配置未生效，导致 UGC 资产被错误排除

### 问题描述

`DirectoriesToNeverCook` 配置项本意是告知 UE4 Cook 流程跳过某些目录（如公共资产），
但实际构建中该配置要么没有被正确写入 INI，要么被 `ReplaceResConfig` 流程覆盖或保留了
不该有的 UGC 路径，最终导致 UGC 关键蓝图资产未被 Cook，游戏进入 UGC 地图时崩溃或卡住。

### Hughes Li 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                                         | 关键发现                                                            |
| ---------- | -------------------------------------------- | --------------------------------------------------------------- |
| [64]       | 修复日志文件中的配置问题                                 | 发现 NeverCook 条目未生效，修复 INI 的 section header 后加载失败数量显著减少          |
| [66]       | 记录待办事项                                       | NeverCook 条目未生效，导致特定蓝图依赖被加载；修复了 INI section header              |
| [74]       | 修复 NeverCook 条目未生效并处理蓝图错误                    | 修复 INI 配置以确保 NeverCook 条目生效，并发现依赖加载问题                           |
| [108]      | 对比配置文件 nevercook 目录与日志                       | DirectoriesToNeverCook 设置并未阻止资源烹饪                               |
| [199]      | 硬链接是否影响公共资产引用                                | DirectoriesToNeverCook 在 Windows 构建中未生效                         |
| [241]      | ReplaceResConfig 如何替换 DirectoriesToNeverCook | 发现 NeverCook 配置问题根本原因，实现 UFS 恢复逻辑                               |
| [323]      | （无明确请求）                                      | UGC 资产未被 Cook，NeverCook 问题已识别，UGC 路径来自 DefaultGame.ini 第 1515 行 |
| [331]      | 确认配置文件不包含 UGC NeverCook 条目                   | NeverCook 设置源自特定配置文件，影响 UGC 资产处理                                |
| [381]      | 游戏内显示下载了 2G 内容                               | 修复 NeverCook 后 UGC 资源能正常下载和使用                                   |


**Observations（关键节选）**

- **[obs 214]** *NeverCook条目未生效的发现*：在当前构建中，发现 NeverCook 条目未生效，需修复 section header 以测试其效果。
- **[obs 218]** *NeverCook方案无法阻止依赖加载*：NeverCook 无法阻止特定蓝图被加载，因为它们被其他正在 Cook 的资产作为依赖引用。
- **[obs 635]** *DirectoriesToNeverCook 在 Windows 构建中未真正生效*：尽管看似能正常运行，CI 流水线与本地构建的处理差异导致构建失败。建议采用"Cook 后 Mount"方法。
- **[obs 698]** *NeverCook 配置问题根本原因识别*：ReplaceResConfig 流程是导致 NeverCook 配置被覆盖的核心原因。
- **[obs 958]** *NeverCook 与 AlwaysCook 配置冲突*：NeverCook CSV 中包含 UGC 路径，而 UGC 的 Feature CSV 又将其列为 AlwaysCook，产生冲突。
- **[obs 985]** *根因分析修正*：UGC 的 NeverCook 设置源自 DefaultGame.ini 第 1515 行，而非 CSV 文件。
- **[obs 1032]** *UGC 资源成功 Cook 打包*：移除 NeverCook 条目后，48,060 个 UGC 资源成功 Cook 并生成。
- **[obs 1177]** *修复 NeverCook 导致 UGC 资源缺失的问题*：确保 UGC 资源在流水线 Cook 阶段能正常处理，避免游戏崩溃。

### Ziyad 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                                                            | 关键发现                                                                            |
| ---------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [273]      | 帮我启动打包                                                          | UGCDoor 的 4 个排除路径中有 3 个缺失，NeverCook 配置未正确包含指定路径                                 |
| [322]      | Cook 命令输出被重定向，无法直接查看                                            | NeverCook 配置按 Feature 目录分别读取，可能未按预期生效                                           |
| [1019]     | 追溯 DefaultGame 配置里 skipgeneratepath、directoriestonevercook 等项来源 | 多处历史溯源查询结果为空，无法定位明确写入来源                                                         |
| [1233]     | 定位并修复 UGC 玩法进入时断言崩溃                                             | **UGC 资源被错误写入 NeverCook，导致角色蓝图未被 Cook，触发断言崩溃**；bat 中修复步骤因前序 PowerShell 返回码问题未执行 |
| [1234]     | UGC 对局启动时 PawnClass 未加载触发崩溃                                     | 已确认崩溃根因是 UGC 目录被写入 NeverCook；fixconfig 阶段会写入该配置，而 cook 阶段不清除                    |
| [1235]     | 进入 UGC 对局时 PawnClass 未加载断言崩溃                                    | 已确认：Cook 阶段不会清掉 NeverCook 配置；bat 修复步骤未生效是因为前序 PowerShell 返回码影响了后续执行流            |
| [1241]     | 是否可以修改 fixconfig 或 nevercook 来阻止 UGC 打入包                        | 未能成功读取相关配置，无法直接判断应改哪个                                                           |
| [1409]     | 澄清问题：不是"崩溃"而是"进不去"，根因是 UGC 资产没被 Cook                            | 当前记忆库中无该问题历史记录；本次明确根因为未 Cook 导致无法进入                                             |


**Observations（关键节选）**

- **[obs 1686]** *检查 NeverCook 配置中的排除路径*：确认 NeverCook 配置是否包含特定排除路径以确保系统正确性。
- **[obs 2000]** *添加排除路径以防止 malformed tag 错误*：5 个排除路径已添加到 FeaturePackConfig-NeverCook.csv。
- **[obs 2041]** *理解 NeverCook 配置逻辑*：之前将排除路径添加到 Community 的 NeverCook 可能不生效，因为该配置按 Feature 目录分别读取。
- **[obs 3338]** *识别配置和语法问题*：DefaultGame.ini 缺失意味着 NeverCook 配置未正常生成；两个关键问题被识别。
- **[obs 3383]** *重构 NeverCook 配置恢复逻辑*：用户优化了 NeverCook 配置恢复的逻辑，提高代码可读性和维护性。
- **[obs 8008]** *在构建日志中搜索 NeverCook 和 UGC 引用*：未发现连接 UGC Cook 排除与 NeverCook 的明确消息。
- **[obs 8067]** *在 DefaultGame.ini 中搜索 NeverCook UGC 路径规则*：对 DefaultGame.ini 进行搜索，验证是否存在 /Game/Feature/UGC 的 NeverCook 规则。

### 共同根因

两人都遭遇了 ** 配置链路不透明** 的问题：

- 配置来源不唯一（DefaultGame.ini + FeaturePackConfig-NeverCook.csv + packConfig.json 三处均可写入）
- / 流程会覆盖或保留不该有的条目
- Cook 阶段本身不清除 NeverCook 配置，只继承上游生成结果
- 结果：UGC 关键蓝图未被 Cook → 游戏进不去 UGC 地图或直接崩溃

---

## 问题二：Cook 过程中 Blueprint 编译错误阻断构建流程

### 问题描述

在 UE4 Cook 阶段，大量蓝图（Blueprint）因依赖缺失、路径错误或 malformed tag
等原因编译失败。早期构建没有容错机制，任何蓝图编译错误都会导致整个 Cook 流程中止。
两人都花费了大量时间排查"是哪些蓝图出错""为何出错""如何容忍/绕过错误继续构建"。

### Hughes Li 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                                             | 关键发现                                                       |
| ---------- | ------------------------------------------------ | ---------------------------------------------------------- |
| [23]       | 抓取 logcat 日志                                     | 成功生成 APK，发现多个蓝图编译错误，解决多进程兼容性问题                             |
| [67]       | 了解编译错误原因                                         | NeverCook 条目未生效导致特定蓝图依赖加载；修复 INI section header 后减少加载失败    |
| [72]       | 修复蓝图加载和编译错误                                      | 修复 INI section header，显著减少加载失败；NeverCook 方案无法阻止依赖加载        |
| [77]       | 了解新旧版本区别                                         | 蓝图编译错误显著减少，集成计划新增错误容忍和长期修复任务                               |
| [118]      | 修改流水线代码                                          | 发现 Cook 过程中 Blueprint 编译错误导致打包问题；通过设置只读属性解决文件被覆盖           |
| [193]      | 修复游戏错误并找到忽略问题的方法                                 | 构建系统存在配置问题，Blueprint 编译错误导致相关模块资产未能生成                      |
| [198]      | 了解流水线主 cook 无法成功的原因                              | "Package is too old"与 CI 流水线分批执行有关，Blueprint 编译错误导致模块资产未生成 |
| [202]      | 找到缺失的 Mod_Community/Mod_System/Mod_OUGCCommon 模块 | 构建系统配置问题，缺失 feature pak 与蓝图编译错误相关                          |
| [211]      | 蓝图编译错误是否与其他问题相关                                  | 蓝图编译错误导致相关模块资产未能生成，修复需在 UE4 编辑器中进行                         |
| [254]      | Cook 的 Blueprint 编译错误及容错机制                       | UFS 配置和恢复逻辑正常工作，确保出现编译错误时能优雅降级                             |
| [315]      | 之前的 Blueprint 编译错误是否被 wrapper 容错处理               | UGC 资源未被烹饪，当前烹饪过程仍在使用 wrapper 的容错机制                        |


**Observations（关键节选）**

- **[obs 223]** *Blueprint 编译错误分析*：分析日志发现蓝图编译错误从 288 个减少到 20 个，构建流程显著改善。
- **[obs 656]** *构建过程分析*：BP 编译错误已完全修复， 成功生成，Lua 模块缺失问题可能已解决。
- **[obs 478]** *新增 DPAR Cook+Pak 步骤*：新增步骤包括临时移除 NeverCook、扫描资产、执行 DPAR Cook、定位 Cooked 文件、创建 Pak 文件及集成到输出目录。

### Ziyad 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                           | 关键发现                                                                   |
| ---------- | ------------------------------ | ---------------------------------------------------------------------- |
| [278]      | 组装 Package 步骤执行                | Cook 过程中错误主要与 malformed tag 相关；外层脚本 type 命令覆盖了 errorlevel，导致错误码未被正确记录  |
| [308]      | 不管这些资产                         | 构建过程存在版本号不一致和日志记录问题，Cook 阶段失败导致后续步骤无法执行；Arena 子目录中存在 malformed tags    |
| [310]      | Cook 非常慢，有办法加速吗                | Cook 阶段失败主要与路径过长和 malformed tag 有关；探讨了 Cook 流程实现和优化机会                  |
| [323]      | （无明确请求）                        | 发现多个编译错误，特别是与 MAYDAY 功能和资产包相关，导致烹饪过程失败                                 |
| [325]      | Cook 失败日志分析                    | 当前命令行缺少关键参数 -IgnoreCookErrors，导致构建失败；mpCook 配置为 true 但命令中没有 -mpCook 参数 |
| [326]      | 这些失败会导致最终目标失败吗                 | 确认 Cook 过程中的错误主要因缺少 -IgnoreCookErrors 参数；分析后确认 Cook 整体不会导致构建失败         |
| [328]      | 设置了 cook 报错忽略，是否会有其他问题         | 启用 -IgnoreCookErrors 参数可忽略个别资产错误继续构建，但可能导致后续视觉内容缺失                     |
| [568]      | 为何 40 个问题会导致另外 500 多个也 Cook 失败 | 扫描失败的包不会被注册；理解了 AssetRegistry 在处理包时的逻辑                                 |
| [1033]     | 了解构建/打包开关与修复的影响                | 多处文件读取为空，无法判断"行尾换行符变化"与"修复是否掩盖 cook 失败"的实际影响                           |


**Observations（关键节选）**

- **[obs 1734]** *Cook 过程失败*：发现 40 个资产，但最终 Cook 失败，错误级别为 1；需分析详细日志。
- **[obs 1984]** *Cook 失败的错误分析和解决建议*：识别出两类主要错误，其中 malformed tag 是主要阻断原因。
- **[obs 2078]** *Cook 过程失败分析*：Cook 从技术上讲是失败的，但通过分析确认某些情况下可容忍继续。
- **[obs 2102]** *分析 Cook 失败原因及解决方案*：Cook 失败根本原因是资产错误，之前的解决方案是取消注释 -IgnoreCookErrors 参数。
- **[obs 2103]** *分析 Cook 失败影响*：确认当前 Cook 进程正在正常运行，有一些警告和保存失败，但整体不会导致构建失败。
- **[obs 3667]** *Cook 失败调查*：557 个 Cook 失败资源，原因是"Package is too old"错误。

### 共同根因

两人都踩到了 **Cook 容错机制缺失/不完善** 的坑：

- Hughes：早期构建没有 ，任何蓝图编译错误直接中止 Cook
- Ziyad：malformed tag 导致 40 个资产扫描失败，进而导致 AssetRegistry 无法注册依赖这些包的 500+ 资产
- 共同问题：**错误码传递链路有缺陷**——外层脚本（bat/PowerShell）的  命令或错误的  读取方式，
使 Cook 失败信号丢失，让流水线误以为成功而继续执行，最终产出不完整的包

---

## 问题三：DefaultGame.ini 被构建流程覆盖，导致手动配置项失效

### 问题描述

 是 UE4 项目的核心配置文件，包含 、
、UGC 地图列表等关键配置。但构建流程中的
 /  步骤会在 Cook 前用仓库内置版本覆盖本地 DefaultGame.ini，
导致手动添加的配置项失效。更严重的是：该覆盖行为不透明、不易追踪，两人都花费大量时间
才找到"配置写了但没生效"的真正原因。

### Hughes Li 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                                                      | 关键发现                                                          |
| ---------- | --------------------------------------------------------- | ------------------------------------------------------------- |
| [30]       | 解决游戏启动过程中公共资产加载失败                                         | DefaultGame.ini 中的配置导致公共资产未被处理；游戏启动被 SDK 初始化失败阻塞              |
| [197]      | 为何当前流水线 defaultgame.ini 生效而 publicassets 未被 Cook          | CI 流水线分批执行导致"Package is too old"；问题与资产引用修复无关                  |
| [236]      | 之前操作是否因 defaultgame.ini 覆盖导致 nevercook 对 publicassets 不生效 | 缺失的 ServerEnum 文件是 Build.py 的配置问题；构建流程增强确保 UFS 路径管理有效         |
| [241]      | ReplaceResConfig 如何替换 DirectoriesToNeverCook              | 发现 NeverCook 配置问题的根本原因，实现了 UFS 恢复逻辑                           |
| [244]      | 在不覆盖 defaultgame.ini 的情况下保存 UFS 路径                        | 发现 UFS 路径保存和恢复的关键问题，分析了 DefaultGame.ini 的修改日志                 |
| [303]      | 确认当前 DefaultGame.ini 是否为 TMR 仓库原始版本                       | 发现 UGC 资产未被构建的原因是配置问题，确认 DefaultGame.ini 的管理状态                |
| [306]      | 确认上次构建是否执行了 ReplaceResConfig                              | ReplaceResConfig 函数未能丢弃 DefaultGame.ini 中的 UGC 条目，可能导致构建失败    |
| [324]      | DefaultGame.ini 文件内容是否被构建流程覆盖                             | NeverCook 设置源自 DefaultGame.ini；当前 BuildTools 不包含 NeverCook 数据 |


**Observations（关键节选）**

- **[obs 75]** *理解 DPAR 设计和 Cook 过程问题*：DefaultGame.ini 中的配置明确指示不处理公共资产；由于挂载点未建立，引用公共资产的主项目资产 Cook 时无法正确解析，出现空引用。
- **[obs 698]** *NeverCook 配置问题根本原因识别*：ReplaceResConfig 流程是导致 NeverCook 配置被覆盖的核心原因。
- **[obs 700]** *修复 Lua 脚本加载问题*：恢复关键路径 ，确保所有子目录的 luac 文件都能正确打包。之前由于 ReplaceResConfig 的替换，只有 StartUp 目录下的 Lua 能加载。
- **[obs 705]** *理解构建配置机制*：QuickGenAPP 模式不处理 PackConfig，保留原始配置；cookpak 模式通过 GetPackConfig 加载 BuildPackInfo 并进行全量替换，导致 DefaultGame.ini 被覆盖。
- **[obs 707]** *UFS 路径保存和 Restored 失败问题识别*：发现 UFS 路径在 DefaultGame.ini 中的差异，以及文件被设为只读后的 bug。
- **[obs 936]** *UGC 资产 Cook 过程问题识别*：ReplaceResConfig 函数保留了 DefaultGame.ini 中的某些条目，导致 UGC 资产未被 Cook。
- **[obs 938]** *DefaultGame.ini 生命周期分析*：ReplaceConfigFile 替换逻辑应丢弃原始文件中的 UGC 条目，但当前文件中仍存在这些条目，提示可能存在构建失败或文件被覆盖。
- **[obs 985]** *根因分析修正*：UGC 的 NeverCook 设置源自 DefaultGame.ini 第 1515 行，而非 CSV 文件。

### Ziyad 侧证据

**Session Summaries（关键节选）**


| Summary ID | 请求摘要                                                            | 关键发现                                                                 |
| ---------- | --------------------------------------------------------------- | -------------------------------------------------------------------- |
| [521]      | 修复                                                              | Cook 过程成功生成 210 个包，但由于错误级别 1 未能执行后续步骤；DefaultGame.ini 文件缺失导致 Cook 失败 |
| [523]      | 本地 build 结束，查看产物                                                | DPAR 未生成新 pak 文件，主要因 Cook 阶段错误；用户确认了 DefaultGame.ini 的语法和错误处理逻辑      |
| [1019]     | 追溯 DefaultGame 配置里 skipgeneratepath、directoriestonevercook 等项来源 | 多处历史/溯源查询结果为空，无法定位到明确写入来源；仓库搜索也未找到脚本或代码引用                            |
| [1231]     | 是否应改动通用的 ReplaceConfig 方案                                       | 现有检索中没有找到目标配置项或稳定的替换痕迹；直接改通用 ReplaceConfig 影响面较大，可能增加回主干的合并成本        |
| [1240]     | 确认之前的 localbuild 脚本没有要求 cook UGC，后来已通过修改构建脚本补上                  | 关键发现：问题在 localbuild 脚本缺少 cook UGC 的要求；用户明确说明并未修改 defaultgame.ini     |


**Observations（关键节选）**

- **[obs 3269]** *Cook 命令执行失败*：日志中发现关键警告，DefaultGame.ini 文件缺失，Cook 过程失败。
- **[obs 3276]** *DPAR 过程失败分析*：DPAR 未生成预期的新 pak 文件，原因是 Cook 阶段错误，尽管成功执行了前面的步骤。
- **[obs 3281]** *修复脚本中的错误处理逻辑*：脚本运行时未能正确处理 Cook 过程中的错误和警告，导致错误级别不准确；修复措施包括在 Cook 后检查产物是否存在来判断失败，并修复 DefaultGame.ini 检查中的潜在问题。
- **[obs 3282]** *修复 Cook ErrorLevel 容错处理和 DefaultGame.ini 路径检测*：两个修复针对 Cook ErrorLevel 的容错处理和 DefaultGame.ini 路径检测，预期能成功生成 121 个 pak 文件。
- **[obs 3338]** *识别配置和语法问题*：DefaultGame.ini 缺失意味着 NeverCook 配置未正常生成；两个关键问题被识别。
- **[obs 7149]** *对 DefaultGame.ini 第 1195-1200 行运行 git blame*：blame 输出为空，说明该文件不被 git 追踪（由构建流程动态生成/覆盖）。
- **[obs 7157]** *检查 DefaultGame.ini 是否被 git 追踪*：git ls-files 结果为空，确认该文件未被 git 管理，由构建流程控制。

### 共同根因

两人都踩到了 **DefaultGame.ini 生命周期不透明** 的坑：

- 该文件既不被 git 追踪，又会被 ReplaceResConfig/fixconfig/Build.py 动态覆盖
- 手动或脚本写入的配置项（如 NeverCook、UFS 路径）会在下次构建时被静默清除
- 两人都尝试用 git blame/git log/git diff 追踪配置来源，均发现文件不受版本控制
- 结果：配置调试形成"改了又被覆盖"的死循环，排查周期拉长到数十次会话

---

## 总结


| 问题                      | Hughes Li 涉及 Session 数 | Ziyad 涉及 Session 数 | 核心症状                           |
| ----------------------- | ---------------------- | ------------------ | ------------------------------ |
| NeverCook 配置未生效         | 40+                    | 30+                | UGC 资产未被 Cook，进 UGC 地图崩溃/卡住    |
| Blueprint 编译错误阻断 Cook   | 30+                    | 20+                | Cook 中止，feature pak 缺失，游戏功能不完整 |
| DefaultGame.ini 被构建流程覆盖 | 15+                    | 15+                | 手动配置失效，排查死循环，构建结果不可预期          |


三个问题在本质上相互交织：

- DefaultGame.ini 被覆盖 → NeverCook 配置被重置 → UGC 资产未被 Cook → 游戏崩溃
- Blueprint 编译错误 + NeverCook 配置冲突 → feature pak 缺失 → 模块加载失败
- 错误码传递缺陷 → 构建流程误判成功 → 问题被掩盖，难以定位

> 文档生成时间：基于 dpar-hughesli.db 和 dpar-ziyad.db 全量数据分析

