# 工程经验叙事

> 自动生成于 2026-04-11 15:32 UTC，基于 Shadow-Folk 记忆数据蒸馏重建。

## UE4 Windows构建：DirectoriesToNeverCook配置失效的跨平台陷阱

### 问题描述

UE4项目在Windows平台执行Cook时，DefaultGame.ini中配置的`DirectoriesToNeverCook`未能生效，原本不应被打包的目录仍然出现在最终包体中。同样的配置在其他平台构建时表现正常。

### 局限环境

UE4, DefaultGame.ini, Windows Build, Cook Pipeline

### 项目

UE4跨平台构建流程

### 处理时间

问题发现于Windows构建验证阶段，尚未完全收敛

### 解决过程

1. [线索] 发现包体中出现不应存在的目录内容，第一反应是配置写错了——检查DefaultGame.ini中`DirectoriesToNeverCook`的路径拼写，确认无误。
2. [失败] 尝试重新触发Cook，问题复现。排除偶发性缓存问题。
3. [转折点] 对比同一份ini在Mac/Linux构建机上的行为，发现其他平台Cook结果正确，目录被正确排除。这说明不是配置本身的问题，而是Windows构建环境对该配置的解析存在差异。
4. [假设] 怀疑Windows路径分隔符（反斜杠`\`）与UE4内部路径格式（正斜杠`/`）不一致，导致路径匹配失败。UE4的Cook流程在做路径比对时，Windows环境下可能未做统一的路径格式化处理。
5. [噪音识别] 曾怀疑是Cook缓存导致旧文件残留，但清空DerivedDataCache后问题依然存在，排除缓存干扰。
6. [当前状态] 根因尚在验证中，临时方案是在Lua层或构建脚本层增加额外的目录过滤逻辑作为兜底，同时在CI的Cook后步骤中增加验证，检查禁止打包的目录是否出现在包体中。

### 解决结果

临时方案：在构建脚本层面增加Cook后目录过滤逻辑作为补充。根本方案待验证：将ini中的路径统一改为正斜杠格式，并在Windows和其他平台分别验证。CI中增加Cook后断言步骤，一旦禁止目录出现即报错阻断流水线。

### 经验提炼

- 跨平台构建配置不要假设在一个平台验证通过就等于全平台有效，每个平台单独跑一遍验证
- ini配置中的路径统一使用正斜杠，不要依赖平台自动转换
- 在CI Cook阶段后增加包体内容断言，而不是靠人工抽查
- 遇到跨平台行为不一致时，先做控制变量——固定配置，只换构建平台，快速定位是环境差异还是配置差异

**方法论标签**: 控制变量法 / 平台差异隔离 / 防御性CI验证 / 配置格式标准化

---

## Android 14 Scoped Storage：adb push Permission Denied的完整排查路径

### 问题描述

在Android 14设备上通过adb将游戏资源.pak文件推送到应用目录时，持续遭遇EACCES（Permission denied）错误。adb push失败，adb shell cp失败，文件管理器也无法访问目标目录，测试流程完全阻塞。

### 局限环境

Android 14设备, adb, .pak文件, /sdcard/Android/data//files/, MTP, MT Manager, SAF

### 项目

com.tencent.letsgo Android测试资源部署

### 处理时间

测试准备阶段，多次尝试后逐步收敛到可行方案

### 解决过程

1. [失败] 直接执行`adb push local.pak /sdcard/Android/data/com.tencent.letsgo/files/`，报`Permission denied`，1 file pushed but failed。第一反应是adb权限问题，检查设备是否开启USB调试——已开启，排除。
2. [失败] 尝试`adb shell cp /sdcard/Download/local.pak /sdcard/Android/data/com.tencent.letsgo/files/`，同样Permission denied。说明不是adb push协议层的问题，shell层也无法写入。
3. [失败] 打开设备上的系统文件管理器，尝试手动复制文件到Android/data目录，提示无权限访问。这是第一个关键信号：连系统文件管理器都被拦截，说明这是OS级别的限制，不是adb的问题。
4. [噪音识别] 曾怀疑是应用未安装或包名写错，通过`adb shell pm list packages | grep letsgo`确认应用已安装，包名正确。排除此方向。
5. [转折点] 查阅Android 14变更日志，发现Android 14强化了Scoped Storage，Android/data/和Android/obb/目录对外部工具（包括adb shell）完全不可写，即使是root模式下的adb也受到限制。这不是配置问题，是系统安全策略。
6. [尝试] 改用MTP协议通过PC文件管理器访问——部分机型可以，但国产品牌手机（如华为、小米）可能额外限制SAF访问，不稳定。
7. [成功路径A] 两步走方案：先`adb push local.pak /sdcard/Download/GameRes/`推送到公共可写目录，再通过MT Manager（具有SAF授权的文件管理器）将文件从Download/GameRes/手动复制到目标私有目录。验证成功。
8. [成功路径B] 对于debug包，使用`adb shell run-as com.tencent.letsgo`进入应用沙盒后操作文件，绕过外部访问限制。
9. [根本方案] 从构建层面解决：直接将必要的pak文件打入APK/OBB，消除运行时手动部署的需求。

### 解决结果

短期：两步走——adb push到/sdcard/Download/，再用MT Manager通过SAF授权复制到应用私有目录。调试包可用`adb shell run-as <package>`操作。长期：构建流程直接内置资源文件，消除运行时手动部署依赖。

### 经验提炼

- 遇到Permission denied先判断是工具权限问题还是OS级安全策略，连系统文件管理器都拦截就说明是后者，不要继续在adb参数上浪费时间
- Android大版本升级后第一件事是跑一遍文件访问相关的测试用例，不要等到测试流程阻塞才发现
- 测试资源部署方案要写进项目文档，标注Android版本对应的可行路径，避免每次测试重新踩坑
- 构建流程能内置的资源就内置，减少运行时手动操作的环节
- 区分'adb层失败'和'OS层拒绝'——前者看adb错误码，后者看系统日志和Android版本变更说明

**方法论标签**: 分层定位（工具层vs系统层） / Android版本变更追踪 / 两步走降级方案 / 构建时解决优于运行时修复

---

## PowerShell语法陷阱全图鉴：&&、&、引号、Unix工具的系统性踩坑记录

### 问题描述

在Windows PowerShell环境中执行各类命令时，反复遭遇ParserError（InvalidEndOfLine）、CommandNotFoundException、ParameterBindingException等错误。涉及git命令链、adb命令、curl JSON请求、Unix工具（rg/wc/head/grep）等多个场景，错误表象各异但根因高度一致：将bash/cmd语法习惯带入了PowerShell执行环境。

### 局限环境

Windows PowerShell 5.x / 7+, Git CLI, ADB, curl.exe, Invoke-RestMethod, ripgrep, cmd.exe

### 项目

跨平台构建脚本、调试工具链、CI Pipeline

### 处理时间

多个独立事件的汇总，每次单独踩坑耗时5-30分钟

### 解决过程

**场景一：&& 操作符导致ParserError**

1. [失败] 执行`git add -A && git commit -m 'msg'`，报ParserError: InvalidEndOfLine。第一反应是引号问题，改了引号格式，还是报错。
2. [转折点] 意识到错误发生在`&&`位置，不是引号。查文档发现：PowerShell 5.x根本不支持`&&`操作符（该特性在PS 7+才引入）。
3. [成功] 拆分为两条独立命令分步执行，问题消失。

**场景二：& 操作符在PowerShell中的语义冲突**

1. [失败] 执行`git branch -a --list '*ziyad*' & git log ...`，报ParserError。
2. [噪音识别] 错误信息指向`&`符号，但`&`在cmd中明明是合法的命令连接符。关键认知：在PowerShell中`&`是调用操作符（call operator），不是顺序执行符，语义完全不同。
3. [成功] 改用分号`;`分隔，或分两次执行。

**场景三：curl内联JSON导致服务端500**

1. [失败] 在PowerShell中执行`curl.exe -X POST -d '{"key":"value"}'`，服务端返回HTTP 500，错误信息'Expected property name or } in JSON at position 1'。position 1报错意味着第一个字符就不合法。
2. [失败] 调整引号转义方式，尝试多种组合（单引号包双引号、反引号转义），服务端仍然报JSON parse error。
3. [转折点] 将同样的JSON写入临时文件，用`curl.exe -d @tmpfile.json`传递，服务端返回200。说明JSON内容本身没问题，是PowerShell在传递字符串给外部进程时消费或错位了引号。
4. [成功] 统一使用临时文件传递JSON body，或改用`Invoke-RestMethod`让PowerShell自己序列化对象。

**场景四：Invoke-RestMethod的Content-Type参数绑定异常**

1. [失败] 执行`Invoke-RestMethod -Uri $url -Headers @{'Content-Type'='application/json'} -Body $body`，报ParameterBindingException: 'Content-Type header could not be converted to the expected format'。
2. [噪音识别] 错误信息像是类型转换问题，但实际上Content-Type在PowerShell HTTP cmdlet中被特殊处理——它不是普通header，有专用参数`-ContentType`。
3. [成功] 改为`Invoke-RestMethod -Uri $url -Method POST -ContentType 'application/json' -Body $body`，问题解决。

**场景五：Unix工具（rg/wc/head/grep）CommandNotFoundException**

1. [失败] 执行`rg 'pattern' logfile`，报CommandNotFoundException。换`wc -l logfile`，同样报错。换`ls path | head -30`，head报错。
2. [认知] 这不是环境配置问题，是根本性的工具链差异：PowerShell不内置Unix工具，也没有这些命令的别名（除了ls=Get-ChildItem）。
3. [成功] 建立对照替换表：`rg`→`Select-String -Recurse`，`wc -l`→`(Get-Content file).Count`，`head -n`→`Select-Object -First n`，`grep`→`Select-String`。注意：Get-Content处理33MB大文件耗时16-19秒，性能敏感场景考虑安装ripgrep。

**场景六：cmd.exe调用PowerShell的续行符和长度限制**

1. [失败] 在批处理脚本中用`^`作为续行符拼接PowerShell命令，执行报错——`^`在双引号内被当作字面字符，不是续行符。
2. [失败] 尝试用`call`创建子进程，但变量展开后命令行超过8191字符（cmd.exe硬限制），命令被截断。
3. [成功] 将完整PowerShell命令写入临时.ps1文件，通过`powershell.exe -File temp.ps1`执行，彻底绕过cmd.exe的解析层和长度限制。

**场景七：adb shell命令在PowerShell中的解析歧义**

1. [失败] 执行`adb shell screencap -p /sdcard/screen.png`报parser error。
2. [成功] 用双引号包裹整个shell参数：`adb shell "screencap -p /sdcard/screen.png"`，或拆分为两步：先screencap，再adb pull。

**场景八：Get-ChildItem枚举符号链接目录返回空**

1. [失败] `Get-ChildItem 'path'`返回空，但`Test-Path`确认目录存在。
2. [噪音识别] 以为是权限问题，检查ACL，权限正常。转而用`cmd /c dir /a`，文件全部显示出来。
3. [转折点] 发现该目录是reparse point（符号链接），`Get-ChildItem`不加`-Force`时跳过特殊属性条目。
4. [成功] 加`-Force`参数，或改用`cmd /c dir /a /s /b`。

### 解决结果

建立PowerShell环境下的命令规范：1) 禁用&&和&作为命令连接符，改用分号或分步执行；2) JSON传递统一用临时文件或Invoke-RestMethod；3) Content-Type用-ContentType参数；4) Unix工具全部替换为PowerShell原生cmdlet；5) 从cmd调用复杂PS命令时写.ps1文件；6) adb shell参数用双引号包裹；7) 枚举目录加-Force参数。

### 经验提炼

- 在PowerShell中看到ParserError先检查是否用了&&或&连接命令，这是最高频的跨shell语法污染
- 向外部进程传递含嵌套引号的字符串时，优先用文件传递而非内联字符串，彻底规避转义地狱
- Invoke-RestMethod的Content-Type必须用-ContentType参数，不能放进-Headers哈希表
- Unix工具（rg/wc/head/grep/awk）在PowerShell中一律不可用，维护一份对照替换表贴在工作区
- 从cmd.exe调用复杂PowerShell命令时，写临时.ps1文件而不是内联字符串，同时规避续行符和8191字符限制
- Get-ChildItem枚举目录时加-Force，防止符号链接目录内容被静默跳过
- adb shell命令在PowerShell中用双引号包裹整个shell参数，防止路径中的特殊字符被PowerShell解析
- 看到CommandNotFoundException不要去找安装包，先问自己'这是Unix工具吗'，是的话直接查PowerShell等效命令

**方法论标签**: Shell语法隔离 / 外部进程参数传递 / 工具链可移植性 / 临时文件绕过转义 / 分层错误定位（Shell层vs工具层）

---

## Android 14兼容性：游戏版本1.0.8.1的多点失效与系统级API变更

### 问题描述

游戏版本1.0.8.1在Android 14设备上出现多个兼容性问题：存储访问限制、后台进程启动失败、显示设置API调用失败。问题不是单点故障，而是Android 14系统级变更在多个维度同时命中了旧版本的适配盲区。

### 局限环境

Android 14, UE4 Android构建, com.tencent.letsgo v1.0.8.1, targetSdk低于Android 14

### 项目

com.tencent.letsgo

### 处理时间

Android 14设备测试阶段发现

### 解决过程

1. [现象] 测试人员在Android 14设备上运行1.0.8.1，报告多个不相关的问题：文件读写失败、某些功能无法启动、界面显示异常。
2. [噪音识别] 初期怀疑是设备特定问题（某品牌Android 14机型的定制ROM），在另一台Android 14原生设备上复现，排除ROM定制干扰。
3. [转折点] 将同一APK安装到Android 13设备，三个问题全部消失。这是关键信号：问题与Android版本强相关，不是代码bug，是系统API变更导致的兼容性断层。
4. [分析] 逐项对应Android 14变更日志：
  - 存储访问失败 → Android 14强制执行分区存储（Scoped Storage），targetSdk低于34时部分行为被系统强制覆盖
  - 后台进程启动失败 → Android 14收紧后台进程启动限制，旧版WorkManager或直接启动Service的方式被拦截
  - 显示设置失败 → 窗口显示相关API在Android 14有Breaking Change，UE的Android适配层尚未跟进
5. [当前状态] UE引擎的Android适配通常滞后于系统发布。短期方案是检查是否有针对Android 14适配的引擎更新版本；长期需要逐项适配。

### 解决结果

短期：检查UE引擎是否有Android 14适配补丁，升级引擎版本。长期：存储访问改用MediaStore/SAF API，后台进程改用WorkManager，显示API按Android 14规范更新。CI增加targetSdk版本检查，新Android版本发布后优先跑兼容性回归。

### 经验提炼

- 多个不相关功能同时在新系统版本上失效，优先怀疑系统API变更而不是代码回归
- Android新版本发布后第一时间在测试设备上跑完整回归，不要等到用户反馈
- UE引擎的Android适配滞后是已知规律，升级系统版本前先确认引擎适配状态
- targetSdk版本要跟着系统版本走，低targetSdk在高版本系统上的行为是不可预测的

**方法论标签**: 版本差异隔离 / 多点失效根因聚合 / 系统变更追踪 / 引擎适配滞后预判

---

## YAML迁移时硬编码字符串替换动态变量，多配置构建路径校验失效

### 问题描述

DPAR Pipeline 在切换构建配置为 Shipping 时，路径校验逻辑判断错误，构建流程异常终止。Development 配置下一切正常，只有切换配置时才复现，表现极具迷惑性。

### 局限环境

DPAR Pipeline, YAML CI Configuration, UE4 Build Configuration, v1 wrapper迁移场景

### 项目

DPAR Pipeline

### 处理时间

配置迁移后首次切换 Shipping 配置时发现，定位耗时约半天

### 解决过程

1. [失败] 首先怀疑 Shipping 配置本身的构建产物路径有变化，逐一比对 UE4 Shipping 和 Development 的输出目录结构，路径结构完全一致，排除。
2. [线索] 对比 v1 wrapper 和新 YAML inline 脚本的行为差异，发现 Development 下两者结果相同，Shipping 下只有 v1 正常——说明问题出在迁移过程本身，而非构建配置逻辑。
3. [转折点] 逐行 diff v1 wrapper 与 YAML inline 脚本，在 DPAR_COOKED_CHECK_DIR 赋值处发现：v1 使用 %TargetBuildConfig% 动态变量，YAML 版本写死了字符串 'Development'。这个改动极可能是迁移时「为了测试方便」临时硬编码后忘记还原。
4. [噪音识别] Development 下路径校验通过，掩盖了硬编码问题——这是典型的「默认值与硬编码值恰好相等」导致的隐性 bug，只有在非默认场景下才暴露。
5. [解决] 将 DPAR_COOKED_CHECK_DIR 的值从 'Development' 改回 %TargetBuildConfig%，Shipping 配置校验恢复正常。

### 解决结果

将 YAML inline 脚本中 DPAR_COOKED_CHECK_DIR 的赋值从硬编码字符串 'Development' 修正为动态变量 %TargetBuildConfig%，与 v1 wrapper 保持一致。

### 经验提炼

- 迁移脚本时逐行 diff 原版与新版，重点检查所有字符串字面量，确认是否应为变量引用
- 在「默认配置下正常、非默认配置下失败」的 bug 排查中，优先对比两种配置下的代码路径差异，而非环境差异
- 临时硬编码测试值在提交前必须还原，迁移类 PR 应强制要求 reviewer 检查所有字面量字符串

**方法论标签**: 配置迁移验证 / 差异对比法 / 隐性默认值陷阱 / 多配置兼容性

---

## TMR WhiteList 默认值缺失 Feature 仓库，导致 286 个 Blueprint 错误和 pak 为空

### 问题描述

流水线执行后，Feature pak 无法生成，Chunk manifest 条目数为 0，同时爆出 286 个 Blueprint 编译错误，Lua 模块加载失败，游戏无法进入主界面。症状面广、错误类型杂，初看像是多个独立问题同时发生。

### 局限环境

TMRTool, TMRManager.py, WhiteList, Chunk Manifest, Blueprint Compiler, UE4 pak 生成流程

### 项目

Feature 模块构建流水线

### 处理时间

流水线上线后首次全量构建时发现，排查耗时约一天

### 解决过程

1. [失败] 最先被 286 个 Blueprint 编译错误吸引，逐一查看错误信息，发现都是「找不到引用资产」类型。尝试在本地重新 Cook，本地正常——说明问题不在 Blueprint 本身，而在构建环境。
2. [失败] 怀疑 Cook 步骤配置有误，检查 Cook 命令参数，未发现异常。
3. [线索] 注意到 p_feature_community_new 的 Chunk manifest 条目数为 0，这不正常——正常应有数千条。顺着这条线往上查，发现 Feature 模块目录在构建机上是空目录。
4. [转折点] 空目录意味着 TMRTool 根本没有同步这些仓库。查看 TMRTool 日志，发现它在处理仓库列表时跳过了所有 Feature 模块仓库（Mod_Community、Mod_OGCCommunityCommon 等），原因是这些仓库不在 TMR_WHITE_LIST 中。
5. [噪音识别] 286 个 Blueprint 错误是次生症状，不是根因。真正的根因是 TMR_WHITE_LIST 默认值只有 5 个核心仓库，Feature 仓库全部缺失。Blueprint 错误、Lua 失败、pak 为空，都是「目录为空」这一个根因的连锁反应。如果从 Blueprint 错误入手逐一修复，会陷入无底洞。
6. [验证] 在流水线配置中新增 15 个 Mod 选项到 WhiteList checkbox，将 6 个关键 Feature 仓库加入默认值，取消注释 TMR_WHITE_LIST 环境变量赋值。重新构建后 Blueprint 错误归零，manifest 条目从 0 增至 4231。

### 解决结果

在流水线配置文件中：1) 向 WhiteList checkbox 新增 15 个 Mod 选项；2) 将 6 个关键 Feature 仓库加入 TMR_WHITE_LIST 默认值；3) 取消注释 TMR_WHITE_LIST 环境变量赋值行。

### 经验提炼

- 遇到大量异构错误同时爆发时，先找「最上游的空/缺失」，而不是逐一处理每个错误症状
- 检查构建产物的数量指标（manifest 条目数、目录文件数）作为快速健康检查，数量为 0 是强烈的根因信号
- 白名单/过滤类配置新增模块时，必须同步更新默认值，否则新模块在未显式配置的流水线上会静默缺失
- 本地正常、CI 异常的 Blueprint 错误，优先查资产同步和目录完整性，而非 Blueprint 本身

**方法论标签**: 根因溯源 / 次生症状识别 / 配置默认值陷阱 / 数量指标健康检查 / 噪音过滤

---

## UE4 Commandlet 中 AssetRegistry 依赖查询返回空，需显式触发同步扫描

### 问题描述

在 UE4 Commandlet 中调用 GetDependencies / GetReferencers 查询资产依赖关系，结果始终为空或严重不完整，但同样的查询在编辑器中运行完全正常。

### 局限环境

UE4 AssetRegistry, Commandlet, GetDependencies, GetReferencers, FAssetRegistryDependencyOptions

### 项目

UE4 资产依赖分析工具

### 处理时间

Commandlet 开发调试阶段，定位耗时约半天

### 解决过程

1. [失败] 首先怀疑查询参数有误，仔细检查 FAssetRegistryDependencyOptions 的 Hard/Soft/Searchable/Managed 各标志位组合，逐一尝试，结果仍为空。
2. [失败] 怀疑资产路径传入有误，加日志打印传入的 FAssetIdentifier，路径格式正确，编辑器下用同样路径可以查到依赖。
3. [线索] 对比编辑器和 Commandlet 两种运行环境的初始化流程，编辑器启动时会自动触发 AssetRegistry 的后台扫描，Commandlet 没有这个步骤。
4. [转折点] 查阅 UE4 源码，发现 AssetRegistry 在 Commandlet 模式下不会自动执行磁盘扫描，依赖图处于未初始化状态——GetDependencies 查的是一张空表，当然返回空。
5. [解决] 在 Commandlet 的 Run() 方法最开头加入 IAssetRegistry::Get().SearchAllAssets(true)，强制同步完成全量资产发现，之后所有依赖查询结果正常。
6. [优化] 用 FAssetRegistryDependencyOptions 按需过滤依赖类型，避免全量依赖图带来的性能开销。

### 解决结果

在 Commandlet 的 Run() 方法中，在任何 GetDependencies/GetReferencers 调用之前，先执行 IAssetRegistry::Get().SearchAllAssets(true) 完成同步扫描，再配合 FAssetRegistryDependencyOptions 过滤所需依赖类型。

### 经验提炼

- 在 Commandlet 环境中使用任何依赖编辑器自动初始化的子系统前，先查源码确认该子系统是否需要手动触发初始化
- 「编辑器正常、Commandlet 为空」的查询问题，优先怀疑子系统初始化时机差异，而非查询逻辑本身
- AssetRegistry 查询前必须调用 SearchAllAssets(true)，将此作为 Commandlet 开发的标准样板代码

**方法论标签**: 环境差异对比 / 子系统初始化时机 / 源码溯源 / Commandlet 开发规范

---

## CI Agent 以 SYSTEM 账户运行，SVN 凭证缓存隔离导致认证失败

### 问题描述

构建机流水线执行 SVN update 时持续报认证失败，但在普通用户账户下手动执行完全正常。重启 Agent、重新配置 SVN URL 均无效。

### 局限环境

SVN, Windows SYSTEM Account, PsExec (Sysinternals), CI/CD Pipeline, Windows 服务账户体系

### 项目

CI/CD 构建流水线

### 处理时间

新构建机接入流水线时发现，排查耗时约2小时

### 解决过程

1. [失败] 首先在普通用户账户下执行 svn update，成功——说明 SVN 服务器、网络、仓库地址均正常，问题在运行账户。
2. [失败] 尝试在 CI Agent 配置中填入 SVN 用户名和密码，部分 CI 系统会将凭证注入环境变量，但 SVN 命令行不从环境变量读取密码，无效。
3. [线索] 查看 CI Agent 的运行账户，确认是 SYSTEM 账户。意识到 Windows 下 SYSTEM 账户的用户 profile 路径是 C:\Windows\System32\config\systemprofile，与普通用户完全隔离，普通用户缓存的 SVN 凭证（存储在 %APPDATA%\Subversion\auth）对 SYSTEM 账户完全不可见。
4. [转折点] 问题不是「凭证错误」，而是「SYSTEM 账户从未缓存过凭证」。解决方向从「修改凭证」转为「以 SYSTEM 身份缓存凭证」。
5. [噪音识别] 普通用户下 SVN 正常这个现象，一开始让人以为是 Agent 配置问题，实际上它是关键线索——说明凭证本身有效，只是缓存位置不对。
6. [解决] 从 Sysinternals 下载 PsExec，用 PowerShell 执行下载并运行：PsExec.exe -s cmd.exe 打开 SYSTEM 权限的 cmd，在该 cmd 中手动执行一次带 --username 和 --password 的 svn 命令，触发凭证写入 C:\Windows\System32\config\systemprofile\AppData\Roaming\Subversion\auth。此后流水线 SVN update 认证正常。

### 解决结果

使用 PsExec -s 打开 SYSTEM 权限的 cmd，在其中手动执行一次带凭证的 svn 命令，将凭证缓存写入 SYSTEM 账户的 profile 目录。后续流水线以 SYSTEM 账户运行时可直接读取该缓存。

### 经验提炼

- CI Agent 认证失败时，先确认 Agent 运行账户，SYSTEM 账户与普通用户账户的凭证存储完全隔离
- 用 PsExec -s 模拟 SYSTEM 账户环境来调试和初始化凭证，这是处理 Windows 服务账户问题的标准手段
- 「普通用户正常、服务账户失败」的认证问题，根因几乎必然是凭证缓存路径隔离，而非凭证本身错误
- 新构建机接入流水线前，在 SYSTEM 账户下预先缓存所有需要认证的外部服务凭证

**方法论标签**: 账户隔离模型 / 环境差异对比 / Windows 服务账户 / 凭证缓存机制

---

## Windows CI 流水线上 multiprocessing 子进程全部崩溃：spawn 模式下的模块重入陷阱

### 问题描述

Windows 构建流水线执行 PakChunkNew 阶段时，dpar_build_wrapper.py 作为子进程被 import，导致脚本顶层的 monkey-patch 和启动逻辑被重复执行。23 个 worker 子进程全部崩溃，主进程陷入无限等待，215081 个 pak 文件处理任务全部失败，流水线挂死。

### 局限环境

Python multiprocessing（Windows spawn 模式），Windows CI/CD Pipeline，dpar_build_wrapper.py，PakChunkNew 打包阶段

### 项目

DPAR 打包流水线

### 处理时间

流水线挂死后介入排查，定位根因约 1 小时，修复验证约 30 分钟

### 解决过程

**步骤 1 — 失败：看主进程日志，以为是任务分发逻辑 bug**
流水线日志显示 PakChunkNew 阶段之后没有任何输出，主进程静默挂死。第一反应是任务队列或调度逻辑出了问题，检查了任务分发代码，没有发现明显异常。

**步骤 2 — 线索：子进程日志揭示真正的崩溃位置**
转去看子进程的独立日志（Windows 上 spawn 模式的子进程有独立输出流），发现 23 个 worker 进程都在启动阶段就抛出了异常，错误堆栈指向 dpar_build_wrapper.py 的顶层代码——monkey-patch 逻辑和主流程启动代码被再次执行了。

**步骤 3 — 失败：怀疑是 monkey-patch 本身的幂等性问题**
初步猜测是 monkey-patch 被调用两次导致状态冲突，尝试给 patch 逻辑加幂等保护（检查是否已 patch 再决定是否执行）。重新触发流水线，子进程依然崩溃。失败。

**步骤 4 — 思维转折：意识到根本原因是 Windows spawn 的 import 机制**
查阅 Python 文档后确认：Linux 上 multiprocessing 默认用 fork，子进程直接复制父进程内存，不会重新执行模块顶层代码；Windows 上强制使用 spawn，子进程是全新的 Python 解释器，启动时会重新 import 主模块，模块顶层的所有代码都会被执行一遍。这不是 monkey-patch 幂等性的问题，而是整个启动逻辑都不应该在 import 时运行。

**步骤 5 — 噪音识别：本地 Linux 环境测试正常是个误导**
之前在 Linux 开发机上跑同一份脚本完全没问题，这一度让人怀疑是 CI 环境配置差异。实际上这正是 fork vs spawn 的经典陷阱——Linux 上的正常掩盖了代码本身对 Windows spawn 模式的不兼容。

**步骤 6 — 成功：用 `if __name__ == '__main__':` 隔离启动逻辑**
将 dpar_build_wrapper.py 中所有启动逻辑（monkey-patch、进程池创建、主流程调用）全部移入 `if __name__ == '__main__':` 块。子进程 import 该模块时，顶层只剩函数和类定义，不再触发任何副作用。重新触发流水线，23 个 worker 正常启动，215081 个任务全部完成。

### 解决结果

在 dpar_build_wrapper.py 中将所有启动逻辑（monkey-patch、multiprocessing Pool 创建、主流程入口）包裹在 `if __name__ == '__main__':` 条件块内。子进程 spawn 时 import 该模块，`__name_`_ 为模块名而非 `'__main__'`，顶层副作用代码不再执行，崩溃消失。

### 经验提炼

- 在 Windows 上使用 multiprocessing 时，必须将所有启动逻辑放在 `if __name__ == '__main__':` 块内，这是强制要求而非风格建议
- 子进程崩溃导致主进程挂死时，优先查子进程的独立日志，不要只盯着主进程输出
- Linux 本地测试正常不代表 Windows CI 没问题——fork 和 spawn 的行为差异会让同一份代码在两个平台上表现截然不同
- 怀疑幂等性问题之前，先确认代码是否根本就不该在当前执行上下文中运行

**方法论标签**: 平台差异陷阱 / 执行上下文隔离 / 子进程日志优先 / 本地环境噪音识别

---

## ADB 启动 Activity 报 'does not exist'：不要猜类名，先查再用

### 问题描述

通过 ADB 命令 `adb shell "am start -n com.tencent.letsgo/com.epicgames.unreal.GameActivity"` 启动 Android 应用时，ActivityManager 报错 Activity class does not exist，应用无法启动。

### 局限环境

ADB，Android ActivityManager，Unreal Engine Android（定制版本），com.tencent.letsgo 包名

### 项目

LetsGo Android 调试

### 处理时间

约 15 分钟

### 解决过程

**步骤 1 — 失败：按 UE 通用命名惯例猜 Activity 类名**
`com.epicgames.unreal.GameActivity` 是 UE 较新版本的标准 Activity 类名，直接用这个名字执行 `am start`，报 Activity class does not exist。

**步骤 2 — 失败：怀疑包名写错，反复确认**
检查了包名 `com.tencent.letsgo`，通过 `adb shell pm list packages` 确认包已安装，包名无误。问题出在类名上。

**步骤 3 — 思维转折：意识到不同 UE 版本或定制项目的 Activity 类名可能不一致**
该项目使用的是经过定制的 UE 版本，Activity 类名可能被改过。与其继续猜，不如直接从系统查。

**步骤 4 — 成功：用 dumpsys 查询实际注册的 MAIN Activity**
执行 `adb shell "dumpsys package com.tencent.letsgo | grep -A 5 'android.intent.action.MAIN'"`，输出显示实际注册的 Activity 为 `com.epicgames.ue4.GameActivityExt`。用正确类名重新执行 `am start`，应用正常启动。

### 解决结果

通过 `adb shell "dumpsys package <package_name> | grep -A 5 'android.intent.action.MAIN'"` 查询应用实际注册的 MAIN Activity 类名，再用 `adb shell "am start -n com.tencent.letsgo/com.epicgames.ue4.GameActivityExt"` 启动，问题解决。

### 经验提炼

- 启动 Android Activity 前，先用 `dumpsys package <pkg> | grep -A 5 'android.intent.action.MAIN'` 查实际类名，不要依赖框架惯例猜测
- ADB 报 'does not exist' 时，先排查包名是否存在（`pm list packages`），再排查类名是否正确（`dumpsys package`），两步分开验证
- 定制版引擎的 Activity 类名可能与官方版本不同，跨项目复用 ADB 命令时必须重新验证类名

**方法论标签**: 查询优于猜测 / 分层排查 / 定制化环境假设验证

---

## DPAR 流水线 3676 个资产 Cook 失败：挂载顺序错误导致依赖缺失

### 问题描述

DPAR 打包流水线 Cook 阶段大量资产报错，3676 个资产无法正常 Cook，无法产出 Pak 文件。

### 局限环境

DPAR 流水线，build.yaml，Unreal Engine Cook，PublicAssets 挂载，CI/CD

### 项目

DPAR 打包流水线

### 处理时间

约 40 分钟

### 解决过程

**步骤 1 — 失败：查 Cook 报错日志，怀疑是资产本身损坏**
Cook 日志显示大量资产找不到依赖资源，第一反应是资产文件损坏或引用路径错误，检查了几个失败资产的引用关系，引用路径看起来都是正确的。

**步骤 2 — 线索：失败资产都依赖 PublicAssets 目录下的资源**
仔细观察失败资产的共同特征，发现它们都引用了 PublicAssets 目录下的公共资源。单独检查 PublicAssets 目录，发现 Cook 执行时该目录根本不存在——挂载还没发生。

**步骤 3 — 思维转折：问题不在资产本身，在于流水线步骤顺序**
翻看 build.yaml，发现 PublicAssets 的挂载配置被写在了 Cook 步骤之后。流水线按顺序执行，Cook 开始时挂载尚未完成，引擎找不到 PublicAssets 下的任何资源，批量失败。

**步骤 4 — 成功：将挂载配置移到 Cook 步骤之前**
在 build.yaml 中将 PublicAssets 挂载步骤调整到 Cook 步骤之前，重新触发流水线，3676 个资产 Cook 全部通过。

### 解决结果

修改 build.yaml，将 PublicAssets 挂载配置从 Cook 步骤之后移动到 Cook 步骤之前，确保 Cook 执行时公共资产目录已挂载可用。

### 经验提炼

- 批量失败时先找失败资产的共同特征，而不是逐个分析单个资产的错误
- 流水线步骤报依赖缺失时，优先检查依赖的挂载或初始化步骤是否在当前步骤之前完成
- 修改 build.yaml 的步骤顺序后，必须验证所有依赖关系的时序正确性，不要只关注单个步骤的配置正确性

**方法论标签**: 共同特征归因 / 时序依赖排查 / 配置顺序问题

---

## 分支切换后 Lua 报错：GameRes 版本路径不匹配

### 问题描述

切换代码分支后游戏启动即崩溃，抛出 Lua 错误。表象是脚本找不到资源文件，但代码本身没有改动。

### 局限环境

Android 设备本地部署，DPAR 构建系统，GameRes 按版本号路径组织（/sdcard/Android/data/com.tencent.letsgo/files/{version}/），版本号格式 major.minor.build.patch

### 项目

LetsGo

### 处理时间

分支切换后首次启动时发现，定位耗时约 30 分钟

### 解决过程

1. [失败] 第一反应是 Lua 脚本本身有问题，逐行检查报错的脚本文件，没有发现语法或逻辑错误。线索：错误指向的是资源加载路径，不是脚本逻辑。
2. [失败] 怀疑是 APK 安装不干净，卸载重装新 APK，问题依旧。线索：APK 换了但设备上 /sdcard 目录的 GameRes 没动。
3. [转折] 注意到错误日志里的路径包含版本号 1.0.8.1，而新 APK 的版本号已经是 1.6.27581.1——这两个数字差距极大，不像是小版本迭代，更像是跨分支的版本跳跃。此时意识到：GameRes 目录是按版本号命名的，旧的 GameRes 还躺在 1.0.8.1 路径下，新 APK 去 1.6.27581.1 路径找资源，当然找不到。
4. [验证] 用 adb shell ls 确认设备上只有 1.0.8.1 目录，1.6.27581.1 目录根本不存在。问题确认。

### 解决结果

重新部署与新分支版本号（1.6.27581.1）匹配的 GameRes 到设备对应路径，Lua 错误消失，游戏正常启动。

### 经验提炼

- 切换分支前先比对新旧版本号，版本号发生跨越式变化时，必须同步重新部署设备上的 GameRes
- 看到资源加载报错，优先检查路径中的版本号是否与当前 APK 一致，而不是先审查脚本逻辑
- 用 adb shell ls 直接验证设备目录结构，比看日志猜测更快

**方法论标签**: 路径版本耦合 / 环境一致性检查 / 从错误路径反推根因

---

## Dolphin 发布流水线失败：版本号重复提交被拒

### 问题描述

蓝盾流水线在 Dolphin 发布步骤失败，错误信息明确：版本号 1.6.27581.1 在指定渠道下已存在，Dolphin 拒绝重复创建。

### 局限环境

蓝盾 CI/CD 流水线，Dolphin 发布平台，版本号唯一性由 Dolphin 平台在渠道维度强制校验

### 项目

LetsGo

### 处理时间

流水线触发后在发布步骤失败，定位耗时约 15 分钟

### 解决过程

1. [线索] 错误信息非常直接：'版本号已存在'。但第一反应是怀疑流水线配置写错了渠道，检查了渠道参数，配置正确。
2. [失败] 尝试直接重试流水线，失败原因相同——重试不会改变版本号，当然还是冲突。这次失败本身是一个有效信号：说明问题不在网络或临时故障，而是状态性冲突。
3. [转折] 去 Dolphin 控制台手动查询该渠道的版本列表，确认 1.6.27581.1 确实已经存在，是上一次成功发布留下的记录。问题根因清晰：流水线没有在发布前做版本号存在性检查，直接提交导致冲突。
4. [验证] 使用新版本号重新触发流水线，发布成功。

### 解决结果

短期：发布前人工确认目标版本号在 Dolphin 对应渠道下不存在。长期：在流水线发布步骤前增加预检 stage，调用 Dolphin API 查询已有版本列表，版本号冲突时提前 fail 并输出明确提示，避免静默重试浪费时间。

### 经验提炼

- 流水线失败后先读错误信息的字面含义，'版本号已存在'就是版本号已存在，不要先怀疑配置
- 重试失败且原因相同，说明是状态性问题而非临时故障，去平台控制台直接查状态
- 在 CI 发布步骤前加版本号存在性预检，把运行时拒绝变成提前失败

**方法论标签**: 幂等性设计 / 预检门控 / 错误信息字面解读优先

---

## Dolphin/Puffer 发布重复触发：版本号冲突的通用处理模式

### 问题描述

流水线重试或开发者重复手动触发发布时，Dolphin 报错版本号已存在，发布被拒绝。与上一条经验本质相同，但触发场景更多样——包括流水线自动重试和人工重复点击。

### 局限环境

Dolphin 工具，Puffer，CI/CD 流水线，版本管理

### 项目

LetsGo

### 处理时间

多次发布操作中重复出现

### 解决过程

1. [噪音识别] 流水线日志显示'发布失败'，部分同学误以为是网络问题或 Dolphin 服务不稳定，反复重试，每次都失败。这是典型的噪音：错误原因是确定性的状态冲突，重试不会解决问题，反而浪费构建资源。识别方式：看错误码和错误描述，'版本号已存在'是业务逻辑拒绝，不是基础设施故障。
2. [转折] 区分两种处理路径：如果是误操作重复发布，去 Dolphin 删除已有的同版本记录再重发；如果是正常迭代，换新版本号。
3. [根本改进] 在 CI 中引入基于构建号或时间戳的自动版本号生成，从源头消除人工版本号冲突的可能性。

### 解决结果

1. 立即处理：删除 Dolphin 上已有的同版本记录，或使用新的递增版本号重新发布。2. 系统改进：CI/CD 流水线自动生成唯一版本号（如 {base}.{build_number} 或 {base}.{yyyyMMddHHmm}），彻底避免冲突。

### 经验提炼

- 区分'基础设施故障'和'业务逻辑拒绝'，后者重试无效，要改输入而不是重试操作
- 让 CI 自动生成唯一版本号，不要依赖人工维护版本号的唯一性
- 删除已有版本记录前确认该版本没有被外部引用，避免引入新的不一致

**方法论标签**: 幂等性设计 / 自动化唯一 ID 生成 / 错误分类：确定性 vs 临时性

---

## APK 构建产物三重缺陷：渠道 ID 为零、Lua 模块缺失、环境配置错误

### 问题描述

构建出的 APK 上线后发现三个独立问题同时存在：channelID 显示为 00000000（应为具体渠道值）；部分 Lua 脚本模块在包内找不到；MSDK 连接的是测试环境而非生产环境。三个问题同时出现，排查时相互干扰。

### 局限环境

Android APK 构建流水线，MSDK，渠道多包工具（walle/VasDolly 类），Lua 脚本资产打包，dev/prod 环境配置文件

### 项目

LetsGo

### 处理时间

QA 验收阶段发现，完整修复耗时约 2 天

### 解决过程

1. [失败] 最初以为是单一问题，优先排查 channelID 异常。检查 MSDK 初始化代码，逻辑正确，参数读取路径也对。线索：问题不在运行时读取，而在构建时写入。
2. [转折1] 用 apktool 解包 APK，直接检查 APK Signing Block，发现渠道信息字段为空——渠道注入步骤根本没有执行。定位到构建脚本中渠道注入工具的调用被注释掉了（或条件分支未命中）。
3. [噪音识别] 排查 Lua 模块缺失时，一度怀疑是代码分支问题（联系到经验1的记忆），但检查分支后发现代码完整。真正原因是打包清单（asset list）里漏掉了该模块的路径配置，属于配置遗漏而非代码问题。识别方式：解包 APK 直接 grep 目标文件名，确认文件不在包内，排除运行时路径问题。
4. [转折2] MSDK 指向测试环境这个问题最隐蔽——测试阶段功能正常，只是连的服务器不对。排查时发现项目有 dev/prod 两套配置文件，构建时通过手动替换文件来切换，这次发布前有人忘记替换。
5. [方法论] 三个问题的共同根因是：构建流程中存在依赖手动操作的步骤，没有被 CI 参数化和自动化。

### 解决结果

1. 修复构建脚本，确保渠道注入工具在正确的构建阶段被调用，并输出注入结果到日志供验证。2. 补全 Lua 模块的打包清单配置，增加构建后验证步骤（解包检查关键文件存在性）。3. 将 dev/prod 配置切换改为 CI 参数控制，通过 buildType 或 flavor 自动选择配置文件，消除手动操作环节。

### 经验提炼

- APK 产物问题优先解包直接检查，不要在运行时日志里猜测构建期的问题
- 构建流程中所有依赖手动操作的步骤都是潜在的发布事故点，逐一自动化
- 多个问题同时出现时，先独立定位每个问题的根因，再找共同模式，避免互相干扰
- 在 CI 构建后增加产物验证步骤：检查 channelID、关键文件存在性、环境配置标识

**方法论标签**: 构建产物直接检查 / 手动操作自动化 / 多故障独立定位 / 构建后验证门控

---

## Windows 开发环境下 ripgrep 缺失的三次碰壁与降级策略

### 问题描述

在 Windows PowerShell 环境中，先后在 C++ 源码目录、插件目录、构建日志文件三个不同场景下执行 `rg` 命令，均报 CommandNotFoundException，导致代码全文检索和日志分析流程中断。

### 局限环境

Windows PowerShell 5.x / PowerShell 7.x，开发机与构建机混用，ripgrep 未预装，无管理员权限快速安装通道

### 项目

LetsGo 游戏项目 C++ 工程 + CI 构建流水线

### 处理时间

分散在三次独立排查会话中，每次中断约 5–10 分钟

### 解决过程

**第一次碰壁（源码搜索场景）**

1. [失败] 直接执行 `rg LetsGoGameInstance` 搜索 C++ 源码，PowerShell 报错：「rg 不是已知的 cmdlet、函数、脚本文件或可运行程序」。第一反应是路径问题，尝试 `where.exe rg` 确认——无输出，工具根本不存在。
2. [线索] 报错信息是 CommandNotFoundException 而非「拒绝访问」或「找不到路径」，说明不是权限问题，也不是 PATH 配置错误，而是工具本身未安装。
3. [失败] 尝试直接运行 `winget install BurntSushi.ripgrep.MSVC`，但构建机策略限制了 winget 的网络访问，安装挂起。此时意识到：在受控构建环境中依赖外部工具安装是不可靠的，应该找内置替代方案。
4. [成功] 改用 `Get-ChildItem -Recurse -Include *.cpp,*.h | Select-String -Pattern 'LetsGoGameInstance'`，完成搜索，结果与 rg 等价。

**第二次碰壁（日志分析场景，同一周内）**

1. [失败] 换了一台机器分析构建日志，习惯性地又敲了 `rg 'ERROR' build.log`，再次 CommandNotFoundException。这次没有再尝试安装，直接转向降级方案。
2. [线索] 两次失败的共同模式已经清晰：Windows 开发/构建机不把 ripgrep 作为标准工具链的一部分，任何依赖 rg 的脚本在跨机器执行时都是脆弱的。
3. [成功] 使用 `Select-String -Path build.log -Pattern 'ERROR'`，完成日志过滤。

**第三次碰壁（CI 自动化脚本中）**

1. [失败] 在自动化测试脚本中硬编码了 `rg` 命令，CI 流水线执行时报同样错误，导致流水线中断。这次影响范围扩大到整条流水线。
2. [转折点] 这次失败让问题从「个人习惯」升级为「团队基础设施风险」——脚本里的工具依赖必须显式声明或使用内置命令。
3. [成功] 将脚本中所有 `rg` 调用统一替换为 `Select-String`，并在脚本头部注释中标注「不依赖非内置工具」。

### 解决结果

放弃在受控 Windows 环境中依赖 ripgrep，统一使用 PowerShell 原生 `Select-String` 命令作为文本搜索标准方案。对于源码搜索：`Get-ChildItem -Recurse -Include *.cpp,*.h | Select-String -Pattern '<keyword>'`；对于日志搜索：`Select-String -Path <file> -Pattern '<pattern>'`。在本地开发机上可选安装 ripgrep 提升体验，但所有共享脚本和 CI 流程必须使用内置命令。

### 经验提炼

- 遇到 CommandNotFoundException 时，先用 `where.exe <tool>` 确认工具是否存在，再判断是 PATH 问题还是未安装问题，避免在错误方向上浪费时间
- 在 CI 和构建脚本中，禁止依赖非系统内置工具，所有外部工具依赖必须在脚本头部显式声明并提供降级方案
- 同一个错误在不同场景下第二次出现时，立即将其升级为「系统性问题」而非「偶发问题」，触发规范化修复
- 将 `rg` 等效替换为 `Select-String` 时，注意 `Select-String` 默认输出包含文件名和行号，与 rg 格式略有差异，管道处理时需调整解析逻辑

**方法论标签**: 工具可用性验证 / 降级策略 / 跨机器可移植性 / CI 脆弱性识别

---

## SVN 分支变更文件集提取：从完整 diff 到 --summarize 的方案收敛

### 问题描述

在 SVN 分支审核流程中，需要自动识别用户分支相对 trunk 改动了哪些文件，以便触发后续依赖分析。初始方案产生了大量无用的 diff 内容，影响后续处理效率。

### 局限环境

SVN 版本控制系统，CI 自动化流水线，依赖分析工具链

### 项目

LetsGo 游戏项目 CI 自动化 / 代码审核流程

### 处理时间

单次排查，约 30 分钟

### 解决过程

1. [失败] 最初使用 `svn diff URL_trunk URL_branch` 获取变更，输出了完整的文件内容差异（unified diff 格式）。问题在于：后续依赖分析只需要「哪些文件变了」，不需要「变了什么内容」。完整 diff 输出量巨大，解析文件路径需要额外的文本处理，且在大型仓库中耗时明显。
2. [线索] 观察 diff 输出结构，注意到每个文件块都以 `Index: <path>` 开头——理论上可以用 grep 过滤，但这是在用复杂方式解决一个本不应该复杂的问题。
3. [转折点] 查阅 svn diff 文档时发现 `--summarize` 参数，其设计目标正是「只输出变更摘要，不输出内容」。这个参数的存在说明这个需求是 SVN 的标准使用场景，不需要自己造轮子。
4. [成功] 使用 `svn diff --summarize URL_trunk URL_branch`，输出格式为每行一个变更文件，前缀为变更类型（A=新增、M=修改、D=删除），直接可解析，无需额外文本处理。将输出管道给依赖分析工具，整个流程简洁清晰。

### 解决结果

使用 `svn diff --summarize <trunk_url> <branch_url>` 获取两个 SVN URL 之间的文件变更集合，输出仅包含变更路径和类型标记，不含文件内容。将该输出作为依赖分析的输入，实现轻量、快速的变更文件识别。

### 经验提炼

- 在使用 CLI 工具处理数据之前，先查阅其文档中的「摘要/summary」类参数，通常工具已经内置了「只要元数据不要内容」的模式
- 当发现自己在用 grep/awk 解析工具输出来提取本应直接可得的信息时，这是一个信号：应该换一个更合适的子命令或参数
- CI 流程中的文本处理步骤越少越好，每增加一个解析环节就增加一个脆弱点

**方法论标签**: 工具参数深度利用 / 数据最小化原则 / CI 流程简化

---

## 从 APK 内部提取配置文件：adb + unzip 组合调试技巧

### 问题描述

调试阶段需要确认已安装 APK 内打包的 MSDKConfig.ini、GRS SDK 服务器配置、GCloud 相关配置与预期一致，但配置文件打包在 APK 内部，无法直接访问，常规做法是解包整个 APK，但这在设备上操作繁琐。

### 局限环境

Android 设备，ADB 工具链，APK（本质是 ZIP 格式），MSDK / GCloud SDK，Windows 开发机

### 项目

LetsGo Android 客户端 MSDK/GCloud 集成调试

### 处理时间

单次调试会话，约 20 分钟

### 解决过程

1. [失败] 第一反应是把 APK 拉到本地再解包：`adb pull <apk_path> ./app.apk && unzip app.apk assets/MSDKConfig.ini`。这个方案可行，但 APK 通常有几百 MB，pull 过程耗时，且在调试循环中需要反复操作。
2. [线索] APK 本质是 ZIP 文件，而设备上已经有 APK 路径。`unzip` 命令支持直接从 ZIP 中提取单个文件（`-p` 参数输出到 stdout），不需要先把整个文件拉下来。
3. [转折点] 意识到可以通过 `adb shell` 在设备上直接执行 unzip，完全不需要把 APK 传到本地。关键是先用 `adb shell pm path <包名>` 获取设备上的 APK 实际路径，再在 shell 内直接操作。
4. [成功] 组合命令：`adb shell "unzip -p $(pm path com.tencent.letsgo | cut -d: -f2) assets/MSDKConfig.ini"` 直接在设备上解压并输出配置文件内容到终端，整个过程秒级完成，无需传输整个 APK。
5. [补充验证] 用 `unzip -l` 替换 `-p` 可以列举 APK 内所有文件，用于确认配置文件是否存在于预期路径，作为提取前的验证步骤。

### 解决结果

使用 `adb shell pm path <包名>` 获取设备上 APK 的实际路径，再通过 `adb shell "unzip -p <apk_path> <内部文件路径>"` 直接在设备上提取特定配置文件内容，无需将整个 APK 传输到本地。列举文件用 `unzip -l`，提取内容用 `unzip -p`。

### 经验提炼

- 调试 APK 内配置时，优先用 `adb shell pm path` + `unzip -p` 在设备上直接提取，避免拉取整个 APK
- 遇到「容器内文件访问」问题时，先判断容器格式（APK=ZIP、JAR=ZIP），再找对应的命令行工具直接操作，不要默认先解包整体
- 用 `unzip -l` 验证文件路径存在后再用 `unzip -p` 提取内容，避免因路径错误导致的静默失败

**方法论标签**: 工具组合 / 就地调试 / 格式本质识别 / 最小数据传输原则

---

## logcat 历史日志污染：清空缓冲区才是第一步

### 问题描述

调试 Android 游戏卡顿问题时，抓取到的 logcat 日志中混有大量历史日志，17MB 的日志文件里充斥着本次启动之前的旧记录，关键事件淹没在噪音里，根本无法定位卡顿发生的时间点。

### 局限环境

ADB + Android logcat + UE4 Android 构建，调试机通过 USB 连接，Windows 宿主机使用 PowerShell 操作

### 项目

UE4 手游 Android 端调试

### 处理时间

单次调试 session，约 30 分钟

### 解决过程

1. [失败] 直接执行 `adb logcat -d > logcat_output.txt`，得到约 17MB 的文件，粗略一看全是日志，以为抓到了。打开一看发现时间戳从几小时前就开始了——历史日志全混进来了，根本分不清哪些是本次卡顿产生的。
2. [线索] 用 PowerShell 检查文件大小：`(Get-Item logcat_output.txt).length / 1MB`，确认 17MB，行数超过 90 万行。这个体量不是本次 14 分钟运行产生的，说明缓冲区里积压了大量历史数据。
3. [转折点] 意识到问题不是「怎么过滤日志」，而是「日志根本就不干净」。logcat 的环形缓冲区会一直保留历史记录，不主动清空就永远带着历史包袱。
4. [成功] 重新建立标准流程：先 `adb logcat -c` 清空缓冲区，再复现问题（等待游戏卡住），然后 `adb logcat -d -v threadtime > logcat_output.txt` 导出，最后用 PowerShell 确认文件大小合理。这次拿到的日志时间范围精确对应本次操作窗口，噪音大幅减少。
5. [噪音识别] 后来发现有时后台还残留着之前启动的 logcat 进程在持续写入，导致文件越来越大。用 `taskkill` 清理后台 logcat 进程后，导出的文件才真正稳定。

### 解决结果

固化为四步标准流程：① `adb logcat -c` 清空缓冲区 → ② 触发复现操作 → ③ 等待足够时间后 `adb logcat -d -v threadtime > logcat_output.txt` 导出 → ④ PowerShell 检查文件大小确认捕获成功，必要时 `taskkill` 清理残留进程。

### 经验提炼

- 抓日志前必须先执行 `adb logcat -c`，否则历史日志会污染本次分析
- 用文件大小做快速健康检查，异常偏大说明缓冲区未清空或有残留进程
- 用 `taskkill` 清理后台 logcat 进程，避免导出文件被持续追加写入
- 调试流程标准化：清空 → 复现 → 导出 → 验证，缺一步都可能让后续分析白费

**方法论标签**: 环境准备先于数据采集 / 噪音隔离 / 流程标准化 / 工具副作用识别

---

## 90 万行 UE 手游日志的分层过滤策略

### 问题描述

游戏运行约 14 分钟产生了 904729 行日志，其中 Error 2707 条、Warning 1208 条、Fatal/Signal 24 条、ANR 1 条，还有多处 OpenGL 渲染错误。面对这个体量，人工阅读根本不可行，需要一套系统化的分析方法。

### 局限环境

ADB logcat 导出文件 + PowerShell 文本处理，UE4 手游 Android 端，日志包含 UE 引擎 Tag 和系统 Tag 混合输出

### 项目

UE4 手游 Android 端性能调试

### 处理时间

单次分析 session，约 1 小时

### 解决过程

1. [失败] 最初尝试直接 grep 关键词「卡顿」「freeze」，没有命中。UE 的日志不会直接写业务语义词，这条路走不通。
2. [失败] 改为直接看 Error 日志，`Select-String 'Error'` 一下子返回 2707 条，仍然无从下手，不知道哪些 Error 是关键的。
3. [线索] 意识到 2707 条 Error 里一定有大量重复——同一个模块反复报错。改变策略：不看单条，先统计 Tag 频次，找出 Top N 高频问题模块。
4. [转折点] 用 PowerShell 按 Tag 分组统计后，发现某几个 Tag 占了 Error 总量的 80% 以上。这才有了优先级——高频 Tag 对应的模块是重点怀疑对象。
5. [噪音识别] OpenGL 渲染错误看起来很吓人，但统计后发现它们均匀分布在整个运行时间线上，不是在卡顿时间点附近集中爆发。判断为背景噪音，暂时搁置。
6. [成功] 最终确立分层分析顺序：Fatal/CRASH/SIGNAL（24 条，最高优先级，直接定位崩溃栈）→ ANR（1 条，精确到时间点，前后 5 秒日志重点看）→ 高频 Error Tag（按频次排序逐一击破）→ Warning 中的性能相关条目（最后看）。每层都用 PowerShell 的 `Select-String` 配合正则完成，输出结果重定向到独立文件便于对比。

### 解决结果

建立标准化六层日志分析流程：1) 统计总量和各级别数量做健康评估；2) 提取 FATAL/CRASH/SIGNAL 条目；3) 按 Tag 统计 Error 频次识别 Top N 问题模块；4) 针对高频 Tag 提取上下文；5) 检查 ANR 时间点前后日志；6) 最后分析 Warning 中的性能条目。全程用 PowerShell 的 `Select-String`、`Group-Object`、`Sort-Object` 组合完成。

### 经验提炼

- 面对海量日志先统计再阅读，用频次分布决定分析优先级，不要直接跳进单条日志
- 按严重程度分层：Fatal > ANR > 高频 Error > Warning，逐层深入不要乱序
- 均匀分布的错误往往是背景噪音，在时间轴上集中爆发的才是真正的事故信号
- 用 PowerShell 将每层分析结果输出到独立文件，便于跨层对比和回溯
- ANR 是时间锚点，拿到 ANR 时间戳后立刻提取前后 5 秒日志做精细分析

**方法论标签**: 分层过滤 / 频次驱动优先级 / 时间轴对齐 / 噪音识别 / 工具链自动化

---

## Puffer 上传从未执行：Pipeline DAG 依赖链断裂排查

### 问题描述

CDN 上的 UGC pak 始终是旧版本，用户反馈新资源没有生效。负责上传的同学确认 Puffer 上传步骤「应该跑了」，但 CDN 上的内容就是没有更新。

### 局限环境

CI Pipeline（DAG 结构），包含「生成 APP」→「签名/发布/归档（非Res）」→「Puffer 上传」的依赖链，以及独立的 `buildtype=Res` 资源上传路径

### 项目

手游发布流水线

### 处理时间

问题持续数个构建周期，排查约半天

### 解决过程

1. [失败] 第一反应是去看 Puffer 上传步骤的日志，但在 Pipeline 界面根本找不到这个步骤的执行记录——它不是失败了，而是压根没有出现在本次构建里。
2. [线索] 既然步骤没有出现，说明不是执行失败，而是根本没有被触发。这把问题从「上传失败」转移到了「触发条件不满足」。
3. [转折点] 去看 Pipeline DAG 结构，发现「Puffer 上传」属于「签名/发布/归档（非Res）」子流水线的一部分，而这个子流水线的触发条件是上游「生成 APP」步骤成功完成。
4. [确认根因] 翻查近期构建历史，发现「生成 APP」步骤已经持续失败了好几个构建周期。每次构建都在这一步卡住，下游整条子流水线从未被触发——包括 Puffer 上传。
5. [噪音识别] 有人提到「上次手动触发过 Puffer」，这是个误导。手动触发的是 `buildtype=Res` 类型的构建，走的是独立的资源上传路径，和「签名/发布/归档」子流水线是两条不同的路。那次上传成功不代表这条链路正常。
6. [成功] 明确了两条路径的区别后，制定了双轨方案：短期内如果只需要更新 CDN 资源，单独触发 `buildtype=Res` 构建即可绕过 APK 生成；长期还是要修复「生成 APP」步骤，让完整流水线恢复正常。

### 解决结果

1. 优先修复「生成 APP」步骤，成功后 Puffer 上传会自动随下游子流水线执行；2) 紧急情况下用 `buildtype=Res` 参数单独触发资源上传，绕过 APK 生成步骤直接更新 CDN。

### 经验提炼

- 步骤「没有出现」和步骤「失败」是两种不同的故障模式，先判断是哪种再排查
- 排查 CI 问题时先看 Pipeline DAG 结构，搞清楚依赖链，再去看单步日志
- 上游步骤持续失败会静默阻断所有下游步骤，不会有明显报错，容易被误判为下游问题
- 区分流水线的不同触发路径（如 Res 类型 vs 完整构建），手动触发成功不等于自动触发链路正常
- 遇到「功能从未执行」类问题，优先检查触发条件和依赖关系，而不是执行逻辑本身

**方法论标签**: DAG 依赖链分析 / 故障模式区分 / 触发条件优先于执行逻辑 / 噪音识别 / 双轨应急方案

---

## UE 资产反向依赖查询：为什么单文件解析走不通

### 问题描述

排查某个 .uasset 资产的引用链时，需要知道「谁引用了我」，但直接解析单个 .uasset 文件无法得到这个答案，导致排查陷入僵局。

### 局限环境

Unreal Engine 4/5，.uasset 二进制格式，Asset Registry，DevelopmentAssetRegistry.bin，编辑器 Reference Viewer

### 项目

UE 资产管理 / 打包流水线

### 处理时间

排查阶段，具体耗时不详

### 解决过程

1. [失败] 第一反应是直接解析 .uasset 文件的二进制结构，试图从中读取引用关系。解析后发现文件内确实存在 Import Table，但仔细核对后发现里面记录的全是「我依赖谁」——即该资产引用的外部资源列表（正向依赖），完全没有「谁依赖我」的反向信息。
2. [线索] 这个发现带来了关键转折：反向依赖本质上是一个全局关系，不可能存储在单个文件里。一个资产被多少人引用，取决于整个工程里所有其他资产的 Import Table 内容——这是一个需要全量扫描后建立索引才能回答的问题。
3. [思维转折] 意识到问题的本质不是「如何解析单文件」，而是「如何获取全局索引」。UE 编辑器本身在启动时会扫描所有资产并建立 Asset Registry，这个索引文件（DevelopmentAssetRegistry.bin）就是反向依赖查询的正确数据源。
4. [成功] 转向两条路径：一是在编辑器内直接使用 Reference Viewer 工具，UI 层面即可可视化双向引用链；二是离线场景下解析 AssetRegistry.bin 文件，通过脚本批量查询反向依赖，适合 CI 流水线中的自动化校验。

### 解决结果

反向依赖查询必须依赖全局 Asset Registry，而非单文件解析。编辑器内用 Reference Viewer，离线/自动化场景解析 DevelopmentAssetRegistry.bin。

### 经验提炼

- 区分正向依赖（Import Table，单文件可读）和反向依赖（全局索引，需 Asset Registry）的数据来源，避免在错误的地方找答案
- 遇到「单文件解析拿不到的信息」时，优先考虑该信息是否属于全局关系，转而寻找全局索引文件
- 离线脚本查询反向依赖时，直接解析 DevelopmentAssetRegistry.bin，不要试图重新扫描整个工程目录

**方法论标签**: 数据来源定位 / 正向/反向关系区分 / 全局索引 vs 局部文件

---

## 打包与校验流水线共用构建机：一场反复踩坑的隔离之旅

### 问题描述

打包流水线与资产校验流水线在同一台构建机上并发运行时，出现测试资产被误 Cook 进包、校验流程读取到打包中间态等问题，稳定性持续下降，排障成本极高。

### 局限环境

蓝盾 CI，UE4 Cook 流程，SVN，Build.py，packConfig.json，dpar_build_wrapper.py，CVM 构建机

### 项目

dpar 打包 / 资产校验流水线

### 处理时间

多次迭代修复，跨越多个构建周期

### 解决过程

1. [失败] 最初两条流水线共用同一台机器，认为只要调度上错开时间窗口就够了。但实际运行中发现并发冲突仍然频繁出现——打包流程会执行 SVN revert、修改 NeverCook 配置、向 Staging 目录写入 Cook 产物，而校验流程恰好也在向 Staging 子目录写入测试资产，两者的写操作在时间上完全没有隔离。
2. [线索] 排查一次「测试资产出现在正式包里」的事故时，发现测试资产的写入时间戳与打包流程的 Cook 窗口完全重叠。这说明问题不是调度频率，而是共享项目目录本身就是定时炸弹。
3. [失败] 尝试在 Build.py 层面加保护逻辑，让 Cook 阶段跳过 Staging 子目录。但随即发现 Build.py 的 Cook 阶段对资源配置类型做了硬编码——始终使用 'Res' 作为资源配置，无论传入的 buildtype 参数是什么值，导致 packConfig.json 中 BuildTypeDiff.App 的差异化配置完全不生效。这个硬编码 bug 让「按 buildtype 区分 Cook 范围」的思路彻底走不通。
4. [噪音识别] 排查过程中曾怀疑是 SVN revert 操作误删了校验流程写入的文件，花了一段时间追查 SVN 操作日志。后来确认 revert 只影响版本控制内的文件，而测试资产写入的是未纳入版本控制的临时目录——这是一个误导性方向，真正的污染路径是 Cook 阶段的目录扫描范围过宽。
5. [思维转折] 意识到在同一个项目目录上做任何软隔离都是在打补丁，根本矛盾是两类流水线的运行前提完全不同：打包流水线需要干净的编译环境和确定性的 Cook 输入；校验流水线需要完整的项目资产、挂载点和可扫描目录。两者天然互斥，不应该共享物理环境。
6. [成功] 通过蓝盾标签将打包任务固定分配到 dpar-build 机器，校验任务分配到独立的 dpar-check 机器（资产校验不涉及编译，普通 CVM 即可满足配置要求，成本可控）。同时配置蓝盾互斥组，确保极端情况下校验流水线只在没有打包任务运行时才执行，作为兜底保障。
7. [修复] dpar_build_wrapper.py 同步更新，拦截 Cook 阶段的异常（此前「生成 APP」步骤因 subprocess.check_call 抛出 Python 异常导致输出目录缺失而失败），改为 wrapper 方案捕获异常并保证目录结构完整性。

### 解决结果

采用「独立校验构建机（dpar-check）+ 蓝盾互斥调度」组合方案，从物理层面隔离两条流水线的工作区，彻底消除共享目录带来的状态污染。同时修复 Build.py 中 Cook 阶段的硬编码问题，使 buildtype 差异化配置能够正确生效。

### 经验提炼

- 共享构建机的「调度错峰」只是缓兵之计，两类流水线运行前提不同时，必须做物理环境隔离
- 排查构建污染问题时，优先对比两条流水线的目录读写范围，找重叠区域，而不是追查具体操作的时序
- 识别噪音：SVN revert 只影响版本控制内的文件，未纳入版本控制的临时目录不受影响，排查时先确认文件是否在版本控制范围内
- Build.py 中任何硬编码的配置选择逻辑都应该优先怀疑，用实际传参验证是否真正生效，不要假设参数被正确透传
- 资产校验流水线不涉及编译，对机器配置要求低，拆分到独立低配 CVM 的边际成本很小，优先拆分而非共用

**方法论标签**: 环境隔离 / 根因 vs 症状区分 / 噪音识别 / 硬编码排查 / 调度互斥

---

## PublicAssets 审核流程选型：Git+SVN 混合方案为什么走不通

### 问题描述

为 PublicAssets 提交审核流程选型时，最初考虑「Git 分支 + SVN 版本号」的混合方案，但在设计阶段就发现该方案存在根本性的覆盖盲区，无法可靠识别新增公共资产。

### 局限环境

Git（主工程），SVN（PublicAssets），资产提交审核流程，依赖分析脚本

### 项目

PublicAssets 提交审核系统

### 处理时间

方案设计阶段，在实施前发现问题

### 解决过程

1. [失败] 初始方案：利用 Git diff 识别变更文件，结合 SVN 版本号做资产定位。这个思路的吸引力在于 Git 的 diff 工具链成熟，且主工程已经在用 Git，看起来可以复用现有基础设施。
2. [线索] 在梳理 PublicAssets 的实际存储位置时发现关键矛盾：PublicAssets 实际存储在 SVN 仓库，不在 Git 仓库里。新增一个公共资产的操作发生在 SVN 侧，Git 仓库完全感知不到这个变更——新增资产不会出现在任何 Git diff 的输出中。
3. [失败] 考虑过一个变通方案：将公共资产临时 copy 到 Git 仓库的某个临时目录，走完审核流程后再同步回 SVN。但这会在 Git 仓库里引入大量二进制 .uasset 文件，Git 对大二进制文件天然不友好（历史膨胀、无法合并），而且两套系统之间的同步逻辑本身就是新的故障点。
4. [思维转折] 核心问题变成了：既然资产本身在 SVN，审核流程的「提交」和「对比」操作也应该在 SVN 内完成，强行引入 Git 只是在增加复杂度而没有带来收益。
5. [成功] 转向纯 SVN 临时分支方案：服务器在 SVN 创建 branches/submissions/{id}/ 临时分支，赋予用户该分支写权限（trunk 保持只读），用户通过标准 SVN 工具提交资产。审核通过后使用 svn diff --summarize 对比分支与 trunk，精确获取变更文件列表，执行依赖分析，最终 merge 到 trunk 并删除临时分支。整个流程在 SVN 内闭环，不引入跨系统同步。

### 解决结果

采用纯 SVN 临时分支方案，利用 SVN 原生的分支、权限控制和 diff 能力完成审核流程，避免 Git+SVN 混合带来的覆盖盲区和同步复杂度。

### 经验提炼

- 选型时先确认「数据实际存在哪里」，工具链应该围绕数据的实际存储位置选择，而不是围绕现有基础设施复用
- 跨系统联动方案（Git+SVN 混合）每增加一个同步节点就增加一个故障点，优先考虑单系统内闭环
- 识别「临时 copy 到另一个系统」方案的风险：二进制文件历史膨胀、无法合并、同步逻辑成为新的维护负担
- svn diff --summarize 是获取分支与 trunk 变更文件列表的精确手段，适合作为审核后依赖分析的输入

**方法论标签**: 数据存储定位优先 / 单系统闭环 vs 跨系统联动 / 方案覆盖完整性验证

---

## 方法论总结

这批经验的核心方法论可以归纳为「环境边界意识」：绝大多数问题都源于将一个执行环境的规则带入了另一个执行环境。PowerShell不是bash，不是cmd；Android 14不是Android 13；Windows路径不是Unix路径。每次遇到莫名其妙的解析错误或权限拒绝，第一个问题应该是「我现在真正运行在哪个环境里，这个环境的规则是什么」。

具体方法论标签的使用频率揭示了几个高价值模式：

1. **临时文件绕过转义**：凡是需要跨Shell边界传递复杂字符串（尤其是含引号的JSON），写文件比内联字符串可靠一个数量级，不要在转义上浪费时间。
2. **控制变量定位跨平台差异**：固定代码，只换平台/系统版本，能快速区分「代码bug」和「环境差异」。同一APK在Android 13正常、Android 14失败，直接锁定系统变更方向。
3. **分层错误定位**：PowerShell报错不等于adb失败，Shell层的警告不等于业务操作失败。要建立明确的成功判断标准，基于业务输出（'X files pushed'）而非Shell退出码。
4. **防御性CI验证**：配置有效性不能靠人工抽查，Cook后断言、targetSdk版本检查、兼容性回归测试都应该是流水线的标准步骤，把人工验证的成本转移到自动化上。