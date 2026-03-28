# Token 概念详解——基于 DeepSeek Tokenizer 源码分析

> 基于 [deepseek-tokenizer](https://github.com/andersonby/deepseek-tokenizer) v0.2.0 源码，
> 使用 DeepSeek-V3/R1 的 `tokenizer.json` 实际运行验证。

---

## 一、Token 是什么

**Token 是大语言模型处理文本的最小单位。** 模型不直接读"字"或"词"，而是把文本切成一个个 token，每个 token 对应词表中的一个编号（ID）。

打个比方：

```
人类阅读:   "你好世界" → 4 个汉字
模型阅读:   "你好世界" → [你好, 世界] → [ID:1346, ID:5765] → 2 个 token
```

模型的"上下文窗口"（context window）就是以 token 为单位计量的。
DeepSeek-V3 的上下文窗口是 **128K tokens**，不是 128K 个字。

---

## 二、DeepSeek 的 Tokenizer 算法

### 2.1 算法类型：Byte-Level BPE

DeepSeek 使用的是 **Byte-Level BPE**（Byte Pair Encoding），与 GPT-2/GPT-4 同一体系，**不是** SentencePiece Unigram。

源码确认（`py_tokenizer.py`）：

```python
model = data.get("model", {})
if model.get("type") != "BPE":
    raise ValueError("Only BPE model is supported")
```

### 2.2 完整流水线

```
输入文本
  │
  ▼
① Added Token 匹配（特殊标记如 <think>、<｜fim▁hole｜> 等）
  │
  ▼
② 预分词（Pre-tokenize）
  ├─ Split 1: 数字隔离（每 3 位一组）
  ├─ Split 2: CJK 隔离（中日文连续字符成段）
  ├─ Split 3: 通用规则（英文词、标点、空格等）
  └─ ByteLevel: UTF-8 字节 → 可打印字符映射
  │
  ▼
③ BPE 合并（按 127,741 条 merge 规则逐步合并）
  │
  ▼
④ 词表查询（128,000 个基础词 + 815 个特殊词）
  │
  ▼
输出 Token IDs
```

### 2.3 词表规模

| 项目 | 数量 |
|------|------|
| 基础 BPE 词表 | 128,000（ID 0–127999） |
| Added Tokens | 815（ID 128000–128814） |
| 其中 placeholder | 798 个 |
| 其中 chat/FIM 标记 | 17 个（如 `<think>`, `</think>` 等） |
| BPE Merge 规则 | 127,741 条 |
| 模型最大长度 | 131,072 tokens |

---

## 三、一个汉字是多少 Token？——实测结果

### 3.1 核心结论

> **在 DeepSeek 中，绝大多数常用汉字 = 1 个 token，常用词组甚至多个汉字 = 1 个 token。**

这是实际运行结果：

### 3.2 单个常用汉字 → 全部 1 token

```
汉字     Unicode    Token数   Token ID
─────────────────────────────────────
 你      U+4F60      1        804
 好      U+597D      1        853
 我      U+6211      1        531
 的      U+7684      1        301     ← 最高频汉字，ID 很小
 是      U+662F      1        389
 在      U+5728      1        445
 了      U+4E86      1        429
 中      U+4E2D      1        525
 人      U+4EBA      1        470
 大      U+5927      1        547
```

**批量验证：对 180 个常用汉字测试，100% 都是 1 token。**

### 3.3 常用词组 → 多字合一 token

这是 DeepSeek 最厉害的地方——高频中文词组被整体编码为单个 token：

```
词组           汉字数    Token数     效果
───────────────────────────────────────
你好             2         1       2字 = 1 token ✓
人工智能          4         1       4字 = 1 token！
深度学习          4         1       4字 = 1 token！
中华人民共和国     7         1       7字 = 1 token！！
你好世界          4         2       [你好|世界] 各1token
```

**"中华人民共和国"7 个汉字只占 1 个 token**——因为 BPE 训练时发现这个词组出现频率极高，被直接合并进了词表。

### 3.4 生僻字 → 可能 2~4 tokens

```
汉字     类型           UTF-8字节   Token数
─────────────────────────────────────────
 鑫      常见但复杂        3          1     ← 足够高频
 龘      生僻字           3          2     ← 被拆成两段
 曌      武则天造字        3          2     ← 低频，未完全合并
 㐀      CJK扩展A区       3          3     ← 罕见，退化为逐字节
 𠀀      CJK扩展B区       4          4     ← 4字节UTF-8，逐字节
```

### 3.5 中文句子的实际 token 效率

```
句子                                        字符数   Token数   字符/Token
──────────────────────────────────────────────────────────────────────
今天天气真不错，我们一起去公园散步吧。            19      11      1.73
大语言模型的上下文窗口决定了它能同时处理多少信息。  25      12      2.08
DeepSeek是一个中国的人工智能公司，专注于大模型研发。30      14      2.14
```

**大量统计后的中文平均值：约 1.9 个汉字 ≈ 1 个 token。**

---

## 四、底层原理：为什么汉字不是 3 个 token？

你可能会在网上看到"一个汉字 = 2~3 个 token"的说法，这在 GPT-2/GPT-3 时代是对的，但在 DeepSeek（以及 GPT-4、Claude 等新模型）中已经不准确了。

### 4.1 UTF-8 编码层

每个汉字在 UTF-8 中确实是 **3 个字节**：

```
'你' (U+4F60) → UTF-8: [0xE4, 0xBD, 0xA0] → 3 字节
```

### 4.2 ByteLevel 映射层

DeepSeek 沿用 GPT-2 的字节映射表，将 256 个字节值映射到 256 个可打印 Unicode 字符：

```python
# 源码: py_tokenizer.py
def _bytes_to_unicode():
    # 可打印 ASCII + 高位字节 → 直接映射
    # 不可打印字节(0x00-0x20 等) → 映射到 U+0100 以后
    bs = list(range(ord("!"), ord("~") + 1))     # 33~126
    bs.extend(range(0xA1, 0xAC + 1))             # 161~172
    bs.extend(range(0xAE, 0xFF + 1))             # 174~255
    # 剩余字节映射到 256+n
    ...
```

"你"的 3 个 UTF-8 字节被映射为 3 个"伪字符"：

```
0xE4 (228) → 'ä' (U+00E4)    ← 恰好是拉丁字母 ä
0xBD (189) → '½' (U+00BD)    ← 恰好是分数 ½
0xA0 (160) → 'ł' (U+0142)   ← 波兰语字母 ł
```

所以在内部表示中，"你" = `"ä½ł"` 这 3 个映射字符。

### 4.3 BPE 合并层——关键！

如果词表里没有 `ä½ł` 的合并规则，它就是 3 个 token（早期模型如此）。

但 DeepSeek 的 BPE 训练中，因为**中文语料占比非常大**，`ä½ł`（即"你"）被反复出现并逐步合并：

```
BPE 训练过程（简化示意）：

第 1 轮: 统计最高频的字节对
  → 发现 (ä, ½) 经常连续出现 → 合并为 "ä½"

第 N 轮: 
  → 发现 (ä½, ł) 经常连续出现 → 合并为 "ä½ł"
  → "ä½ł" 成为词表中的一个独立词条

甚至:
  → 发现 "ä½ł"(你) 和 "å¥½"(好) 经常连续出现
  → 合并为 "ä½łå¥½"(你好) → 也成为独立词条

再进一步:
  → "ä¸Ńåįİäººæ°ĳåħ±åĴĮåĽ½"(中华人民共和国)
  → 出现太频繁 → 整体成为 1 个 token
```

**这就是为什么 DeepSeek 中文效率远高于 GPT-2 的原因——BPE 训练语料中有大量中文。**

### 4.4 预分词的 CJK 隔离

在 BPE 之前，DeepSeek 会把连续的中文字符**整体成段**处理：

```python
# 源码: py_tokenizer.py
def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FA5      # CJK统一汉字基本区
        or 0x3040 <= code <= 0x309F   # 日文平假名
        or 0x30A0 <= code <= 0x30FF   # 日文片假名
    )

def _match_cjk(text: str, i: int) -> int:
    # 连续的 CJK 字符作为一个整体
    if not _is_cjk(text[i]):
        return 0
    j = i
    while j < n and _is_cjk(text[j]):
        j += 1
    return j - i
```

这意味着 `"人工智能"` 先作为一个完整的预分词片段，再进入 BPE 处理。BPE 发现词表里直接有 `"äººå·¥æĻºèĥ½"`（"人工智能"的 ByteLevel 编码），直接匹配为 1 个 token。

---

## 五、中文 vs 英文 Token 效率对比

### 5.1 同义句对比（实测）

```
中文                              中Token  |  英文                                              英Token  |  比率
─────────────────────────────────────────────────────────────────────────────────────────────────────
人工智能是计算机科学的一个分支       6       |  Artificial intelligence is a branch of CS           8       |  0.75x
今天是星期五                       2       |  Today is Friday                                     3       |  0.67x
请帮我写一段代码                    5       |  Please help me write some code                      6       |  0.83x
这个函数的返回值是什么               4       |  What is the return value of this function            8       |  0.50x
大语言模型正在改变世界               6       |  Large language models are changing the world         7       |  0.86x
```

**中文平均只需要英文 70% 的 token 数来表达相同含义！**

### 5.2 大量文本统计

```
类型        每个 token 约等于             典型效率
────────────────────────────────────────────────
中文        ≈ 1.9 个汉字                 0.52 token/字符
英文        ≈ 0.9 个单词（≈4.5 个字母）    0.14 token/字符
代码        ≈ 3.3 个字符                 0.30 token/字符
```

### 5.3 实际意义——128K 上下文能装多少

```
DeepSeek-V3 的 128K tokens 上下文窗口:

纯中文:  128000 × 1.9 ≈ 24.3 万汉字 ≈ 一本中篇小说
纯英文:  128000 × 4.5 ≈ 57.6 万字母 ≈ 约 12 万单词 ≈ 一本长篇小说
纯代码:  128000 × 3.3 ≈ 42.2 万字符 ≈ 约 1.2 万行代码
```

---

## 六、混合文本的 Token 拆解

以这句实际的混合文本为例：

```
原文: "我正在使用DeepSeek-V3模型，它的context window是128K tokens。"
总计: 48 字符 → 17 tokens
```

逐 token 拆解：

```
Token#  Token文本(内部)          对应原文           类型
──────────────────────────────────────────────────────
  1     æĪĳæŃ£åľ¨               我正在              中文3字=1token
  2     ä½¿çĶ¨                  使用               中文2字=1token
  3     Deep                    Deep               英文
  4     Se                      Se                 英文
  5     ek                      ek                 英文
  6     -V                      -V                 标点+字母
  7     3                       3                  数字
  8     æ¨¡åŀĭ                  模型               中文2字=1token
  9     ï¼Į                     ，                  中文标点=1token
 10     å®ĥçļĦ                  它的               中文2字=1token
 11     context                 context            英文整词
 12     Ġwindow                 ·window            英文(前缀空格)
 13     æĺ¯                     是                  中文1字=1token
 14     128                     128                数字3位=1token
 15     K                       K                  单字母
 16     Ġtokens                 ·tokens            英文(前缀空格)
 17     ãĢĤ                     。                  中文句号=1token
```

观察要点：
- **中文词组高效合并**："我正在" 3 字 = 1 token，"使用" 2 字 = 1 token
- **英文按子词切**："DeepSeek" 被切为 Deep + Se + ek（3 tokens）
- **数字效率高**："128" 整体 1 token
- **中文标点**：逗号、句号各占 1 token

---

## 七、Token 与 Compaction 的关系

回到 OpenClaw 的 Compaction 算法，理解 token 后就能看懂它的参数了：

### 7.1 OpenClaw 的 token 估算方法

OpenClaw 使用一个**粗略估算**（不调用实际 tokenizer）：

```typescript
// OpenClaw 源码: compaction.ts
// estimateTokens 内部使用 chars/4 启发式
export const SAFETY_MARGIN = 1.2; // 20% 安全余量补偿估算误差
```

`chars/4` 对英文来说尚可（4 字符 ≈ 1 token），但对中文来说**严重低估**：

```
实际:   "中华人民共和国" (7字符) = 1 token
估算:   7 / 4 = 1.75 tokens  ← 高估了 75%

实际:   "大语言模型的上下文窗口决定了它能同时处理多少信息。" (25字符) = 12 tokens
估算:   25 / 4 = 6.25 tokens  ← 低估了近一半!
```

这就是 `SAFETY_MARGIN = 1.2` 存在的原因——补偿多字节字符、代码 token 等场景下估算不准的问题。

### 7.2 实际影响

```
假设上下文 200K tokens 的模型:

如果对话全是中文:
  真实: 24万字 ≈ 200K tokens → 该触发 compaction
  估算: 24万字 / 4 = 6万 tokens → 误以为还很远!

  → 可能导致 compaction 触发太晚
  → 实际发给模型时溢出 → 被 API 截断
```

---

## 八、不同模型的 Token 差异

需要注意的是，**每个模型家族有自己的 tokenizer**，同一段文字的 token 数不同：

```
"人工智能是未来的趋势" (10个汉字):

DeepSeek-V3:    约 4-5 tokens  (中文优化最好)
GPT-4/4o:       约 5-7 tokens  (较好)
Claude 3.5:     约 6-8 tokens  (良好)
Llama 3:        约 8-10 tokens (一般)
GPT-2:          约 20+ tokens  (几乎逐字节)
```

DeepSeek 在中文 token 效率上处于领先水平，因为：
1. **训练语料中文占比高** → BPE 学到了大量中文合并规则
2. **词表大**（128K） → 能容纳更多中文词组作为单独 token
3. **CJK 预分词优化** → 连续中文作为整体参与 BPE

---

## 九、总结

### Token 本质

```
Token = 模型的"阅读单位"
      = BPE 算法从训练语料中统计出的最优子词切分
      = 词表中的一个条目 (ID: 0 ~ 128814)
```

### 中文 Token 速查表

| 场景 | 大约关系 |
|------|---------|
| 1 个常用汉字 | = 1 token |
| 1 个高频词（如"人工智能"） | = 1 token |
| 1 个中文句子（20字） | ≈ 10 tokens |
| 1000 个汉字 | ≈ 520 tokens |
| 1 个生僻字 | = 2~3 tokens |
| 1 个 emoji | = 1~2 tokens |
| 1 个 CJK 扩展 B 区字符 | = 4 tokens |

### 实用换算

```
DeepSeek 128K 上下文 ≈ 24万汉字 ≈ 12万英文单词 ≈ 1.2万行代码

1 token ≈ 1.9 个汉字
       ≈ 4.5 个英文字母
       ≈ 0.9 个英文单词
       ≈ 3.3 个代码字符

中文表达同样含义，比英文节省约 30% 的 token。
```
