# nano-GPT Walkthrough 中文直译

本文是 Brendan Bycroft 的 `LLM Visualization` 中 nano-GPT walkthrough 文案的中文直译整理。目标是尽量按网页每一步的说明顺序翻译原文，而不是重新解释模型原理。

相关文件位于本地源码：

- `llm-viz/src/llm/walkthrough/Walkthrough00_Intro.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough01_Prelim.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough02_Embedding.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough03_LayerNorm.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough04_SelfAttention.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough05_Softmax.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough06_Projection.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough07_Mlp.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough08_Transformer.tsx`
- `llm-viz/src/llm/walkthrough/Walkthrough09_Output.tsx`

## 1. Introduction

欢迎来到 GPT 大语言模型的 walkthrough。在这里，我们将探索 `nano-gpt` 这个模型，它只有大约 85,000 个参数。

它的目标很简单：接收一个由 6 个字母组成的序列，例如：

```text
CBABBC
```

并把它按字母顺序排序，也就是得到：

```text
ABBBCC
```

我们把这些字母中的每一个都称为一个 `token`。模型中不同 token 的集合构成了它的 `vocabulary`，也就是词表。

在这个例子中，词表可以理解为：

```text
token: A  B  C
index: 0  1  2
```

从这个表中，每个 token 都被分配了一个数字，也就是它的 `token index`。现在我们就可以把这串数字输入到模型中。

在 3D 视图里，每个绿色单元格代表一个正在被处理的数字，每个蓝色单元格代表一个权重。

序列中的每个数字首先会被转换成一个包含 48 个元素的向量。这个大小是针对当前模型选择的。这个过程称为 `embedding`。

随后，embedding 会被传入模型，经过一系列被称为 `transformer` 的层，最终到达模型底部。

那么输出是什么？它是对序列中下一个 token 的预测。因此，在第 6 个位置，我们会得到下一个 token 是 `A`、`B` 或 `C` 的概率。

在这个例子中，模型非常确定下一个 token 会是 `A`。现在，我们可以把这个预测结果重新输入到模型顶部，并重复整个过程。

## 2. Preliminaries

在深入算法细节之前，我们先稍微后退一步。

这个指南关注的是 `inference`，也就是推理，而不是训练。因此，它只是整个机器学习过程中的一小部分。

在我们的例子中，模型的权重已经预先训练好了，我们使用推理过程来生成输出。这个过程直接在你的浏览器中运行。

这里展示的模型属于 GPT，也就是 `generative pre-trained transformer`，生成式预训练 Transformer 家族。它可以被描述为一种“基于上下文的 token 预测器”。

OpenAI 在 2018 年提出了这个家族，其中比较著名的成员包括 GPT-2、GPT-3 和 GPT-3.5 Turbo，后者是广泛使用的 ChatGPT 的基础。它也可能与 GPT-4 有关，不过具体细节并不公开。

这个指南受到了 `minGPT` GitHub 项目的启发。`minGPT` 是 Andrej Karpathy 创建的一个基于 PyTorch 的最小 GPT 实现。

他的 YouTube 系列 `Neural Networks: Zero to Hero` 以及 `minGPT` 项目，对这个指南的创建非常有帮助。这里展示的玩具模型基于 `minGPT` 项目中的一个模型。

好了，让我们开始吧。

## 3. Embedding

我们前面看到，token 是如何通过一个简单的查找表映射成一串整数的。

这些整数，也就是 `token indices`，是我们在模型中第一次、也是唯一一次看到整数。从这里开始，我们使用的都是浮点数，也就是小数。

现在，让我们看看第 4 个 token，也就是 index 3，是如何被用来生成 `input embedding` 中第 4 列向量的。

我们使用 token index，在这个例子中是：

```text
B = 1
```

来选择左侧 `token embedding matrix` 的第 2 列。注意，这里使用的是从 0 开始的索引，所以第一列的 index 是 0。

这会产生一个大小为 `C = 48` 的列向量，我们称之为 `token embedding`。

因为我们看的 token `B` 位于第 4 个位置：

```text
t = 3
```

所以我们会取 `position embedding matrix` 的第 4 列。

这同样会产生一个大小为 `C = 48` 的列向量，我们称之为 `position embedding`。

注意，position embedding 和 token embedding 都是在训练过程中学到的。在可视化中，它们以蓝色表示。

现在我们有了这两个列向量，只需要把它们相加，就会得到另一个大小为 `C = 48` 的列向量。

现在，我们会对输入序列中的所有 token 运行同样的过程，创建一组同时包含 token 值和位置信息的向量。

你可以把鼠标悬停在 `input embedding` 矩阵中的单个单元格上，查看它们的计算方式和来源。

我们可以看到，对输入序列中的所有 token 运行这个过程后，会生成一个大小为：

```text
T x C
```

的矩阵。

其中 `T` 表示 `time`，也就是时间。你可以把序列中更靠后的 token 理解为更晚的时间。

`C` 表示 `channel`，但它也经常被称为 feature、dimension 或 embedding size。这个长度 `C` 是模型的几个超参数之一，由模型设计者在模型大小和性能之间权衡后选择。

这个矩阵就是我们所说的 `input embedding`，现在它已经准备好向下传入模型。

这组由 `T` 列组成、每一列长度为 `C` 的向量，会在整个指南中反复出现。

### 译注：position embedding matrix 从哪里来

`position embedding matrix` 是模型训练出来的一张参数表，不是输入文本里天然带的，也不是推理时临时算出来的。

它的作用是告诉模型：每个 token 在序列中的位置。

比如输入：

```text
C B A B B C
```

它们的位置可以写成：

```text
0 1 2 3 4 5
```

模型会做两次查表：

```text
token embedding matrix:
根据 token 是 A/B/C 查出“这个字母本身”的向量

position embedding matrix:
根据位置 0/1/2/3/4/5 查出“这个位置”的向量
```

然后相加：

```text
第 t 个输入向量 =
token_embedding[token_id] + position_embedding[t]
```

所以如果第 4 个 token 是 `B`，位置是 `t = 3`：

```text
token_embedding[B]     -> 一个 48 维向量
position_embedding[3]  -> 一个 48 维向量
两者相加               -> 第 4 列 input embedding
```

为什么需要它？因为 Transformer 的 self-attention 本身不天然知道顺序。没有 position embedding，模型看到的更像是一袋 token，不容易区分：

```text
我 爱 你
你 爱 我
```

它们包含的 token 类似，但顺序不同，含义不同。

### 译注：position embedding matrix 的形状

在这种 learned absolute position embedding 设计里，`position embedding matrix` 的形状通常是：

```text
max_seq_len x C
```

其中：

- `max_seq_len` 是模型支持的最大位置数，也可以理解为训练时设置的最大上下文长度。
- `C` 是每个 token 的向量维度，也就是 hidden size / embedding size。

在这个 nano-GPT 可视化里：

```text
T = 11
C = 48
position embedding matrix = 11 x 48
```

意思是模型准备了 11 个位置的 position embedding：

```text
位置 0  -> 48 维向量
位置 1  -> 48 维向量
位置 2  -> 48 维向量
...
位置 10 -> 48 维向量
```

如果是 GPT-2 small 这种早期 GPT 风格，典型配置是：

```text
max_seq_len = 1024
C = 768
position embedding matrix = 1024 x 768
```

所以它和上下文窗口大小直接相关。更准确地说，它对应的是模型训练时支持的最大位置数。如果模型使用这种 learned absolute position embedding，超过这个长度的位置没有对应的已训练位置向量，通常不能直接外推。

### 译注：DeepSeek-V4 使用的是不是这种 position embedding

DeepSeek-V4 / DeepSeek-V4-Pro 不是 nano-GPT 这种显式的：

```text
position_embedding_matrix = max_seq_len x hidden_dim
```

公开资料显示，它使用的是 `Partial RoPE`，也就是部分旋转位置编码。

可以这样对比：

```text
nano-GPT / GPT-2 早期风格:
token embedding + position embedding table

DeepSeek-V4:
不直接查一张位置向量表
而是在 attention 中对 Q / KV 的一部分维度使用 RoPE 注入位置信息
```

RoPE 的核心思想是：根据 token 的位置，对 Q/K 相关向量做位置相关的旋转。这样当模型计算 attention 相似度时，位置信息会自然进入 `Q · K` 的结果中。

DeepSeek-V4 的资料中提到，它采用 Partial Rotary Positional Embedding，RoPE 只作用在 Q / KV 的最后 64 维，而不是作用在全部 hidden 维度上。同时，DeepSeek-V4 还结合了 hybrid sparse attention、compressed KV、sliding window attention 等长上下文机制，用来支持百万级 token 上下文。

参考资料：

- [DeepSeek-V4 on Day 0: From Fast Inference to Verified RL with SGLang and Miles](https://www.lmsys.org/blog/2026-04-25-deepseek-v4/)
- [DeepSeek_V4.pdf](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/resolve/main/DeepSeek_V4.pdf)

## 4. Layer Norm

上一节中的 `input embedding` 矩阵是第一个 Transformer block 的输入。

Transformer block 的第一步，是对这个矩阵应用 `layer normalization`。这是一个会分别归一化矩阵中每一列数值的操作。

归一化是深度神经网络训练中的一个重要步骤，它有助于提高模型在训练过程中的稳定性。

我们可以把每一列分开来看。现在先关注第 4 列：

```text
t = 3
```

目标是让这一列中的平均值等于 0，标准差等于 1。为此，我们会先为这一列找到两个量：

```text
mean (μ)
std dev (σ)
```

然后减去平均值，再除以标准差。

这里我们使用 `E[x]` 表示平均值，使用 `Var[x]` 表示方差，也就是这一列长度为 `C` 的向量的方差。方差就是标准差的平方。

epsilon 项：

```text
ε = 1 x 10^-5
```

用于防止除以 0。

因为这些值会被应用到这一列中的所有元素，所以我们会在 aggregation layer 中计算并存储它们。

最后，一旦得到了归一化后的值，我们会把这一列中的每个元素乘以一个学到的权重：

```text
weight (γ)
```

然后加上一个学到的偏置：

```text
bias (β)
```

得到 `normalized values`。

我们会对 `input embedding matrix` 的每一列运行这个归一化操作，得到 `normalized input embedding`。它已经准备好被传入 Self-Attention 层。

### 译注：DeepSeek-V4 中有没有 LayerNorm

有类似作用的“归一化层”，但公开资料中 DeepSeek-V4 / DeepSeek-V4-Pro 更明确提到的是 `RMSNorm`，也就是 Root Mean Square Layer Normalization，而不是 nano-GPT 这里演示的完整 `LayerNorm`。

二者目的很接近：都是在向量进入后续计算前，把数值尺度控制在更稳定的范围内，避免深层网络训练时激活值忽大忽小，让 attention、MLP 和残差路径更容易稳定学习。

区别可以粗略理解为：

```text
LayerNorm:
先减去平均值，再除以标准差，然后乘以可学习权重 γ，加上可学习偏置 β

RMSNorm:
通常不减平均值，只用均方根 RMS 对向量做缩放，然后乘以可学习权重
```

所以 RMSNorm 可以看作 LayerNorm 的一个更轻量版本。它少做一步均值中心化，计算更简单，在很多现代大模型中很常见。

DeepSeek-V4 的资料中还提到，它会在 attention 计算前对 Q 和压缩后的 KV 条目应用 RMSNorm。这样做的直接目的，是防止进入 `Q · K` 的向量尺度过大，导致 attention logits 爆炸；对于长上下文和很深的 Transformer 来说，这类数值稳定性尤其重要。

需要注意的是，归一化和位置编码不是一回事。前面讲的 `Partial RoPE` 负责把位置信息注入 attention；这里的 RMSNorm / LayerNorm 负责稳定向量的数值尺度。

参考资料：

- [DeepSeek_V4.pdf](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/resolve/main/DeepSeek_V4.pdf)
- [Decoding DeepSeek-V4](https://outcomeschool.com/blog/decoding-deepseek-v4)

## 5. Self Attention

Self-attention 层也许是 Transformer 和 GPT 的核心。它是 `input embedding matrix` 中各列彼此“交谈”的阶段。

到目前为止，在所有其他阶段中，各列基本都可以被看作是彼此独立的。

Self-attention 层由多个 head 组成。现在我们先关注其中一个 head。

第一步是从 `normalized input embedding matrix` 的每一个 `T` 列中生成三个向量：

```text
Q: Query vector
K: Key vector
V: Value vector
```

为了生成这些向量中的一个，我们会执行一次矩阵-向量乘法，并加上一个 bias。

每个输出单元格都是输入向量的某种线性组合。例如，对于 `Q vectors`，它是通过 `Q-weight matrix` 的一行和 `input matrix` 的一列做点积来完成的。

点积操作非常简单，我们会在后面经常看到它：

1. 把第一个向量中的每个元素和第二个向量中对应位置的元素配对。
2. 把每一对相乘。
3. 再把这些乘积加起来。

这是一种通用且简单的方法，可以确保每个输出元素都能受到输入向量中所有元素的影响，而这种影响由权重决定。因此，它在神经网络中频繁出现。

我们会为 Q、K、V 向量中的每一个输出单元格重复这个操作。

那么，我们要用 Q、K、V 向量做什么？

它们的命名给了我们一个提示：`key` 和 `value` 很像软件中的字典，其中 key 映射到 value。然后 `query` 就是我们用来查找 value 的东西。

软件类比：

```text
lookup table:
table = { "key0": "value0", "key1": "value1", ... }

query process:
table["key1"] => "value1"
```

在 self-attention 中，我们不是返回单个条目，而是返回这些条目的某种加权组合。

为了找到这个权重，我们会让一个 Q vector 和每个 K vector 做点积。然后对这些权重进行归一化，最后用它乘以对应的 V vector，并把它们全部加起来。

可以写成：

```text
w0 = Q · K0
w1 = Q · K1
w2 = Q · K2

[w0n, w1n, w2n] = normalization([w0, w1, w2])

result = w0n * V0 + w1n * V1 + w2n * V2
```

### 译注：为什么会有 KV cache

从上面的字典类比看，`K` 就像每条已有记录的索引，`V` 就像这条记录真正能返回的内容，`Q` 则是下一次查询时带来的搜索请求。

在自回归生成中，模型一次只生成一个新 token。生成下一个 token 时，历史 token 的向量内容已经确定了，因此它们对应的 `K` 和 `V` 也不会再变。于是可以把这些历史 token 的 `K/V` 缓存起来：

```text
已有 token:
缓存 K = 每条历史记录的索引
缓存 V = 每条历史记录的内容

新 token:
只计算新的 Q/K/V
用新的 Q 去查询缓存中的历史 K
再按权重读取缓存中的历史 V
```

所以你可以直觉地理解为：每条已有记录的“索引”和“内容”已经缓存好了，下次查询时不需要重新为所有历史记录生成一遍索引和内容。

这里说的“不需要重新加载”，更准确地说是不需要重新计算历史 token 的 `K/V`。它不是数据库意义上的从磁盘重新加载，而是避免每生成一个 token 都把整段历史重新跑一遍 attention 前面的投影计算。

KV cache 之所以只缓存 `K/V`，而通常不缓存旧 token 的 `Q`，是因为下一步生成时真正有用的是“新 token 的 Q 去查历史 K/V”。旧 token 的 Q 已经完成了它当时那一步的查询任务，后面通常不再需要。

为了看一个更具体的例子，让我们看第 6 列：

```text
t = 5
```

我们会从这一列发起 query。

查找中的 `{K, V}` 条目是过去的 6 列，而 Q 值是当前时间。

我们首先计算当前列：

```text
t = 5
```

的 Q vector 和前面每一列的 K vector 之间的点积。然后这些值会被存储到 `attention matrix` 中对应的行，也就是：

```text
t = 5
```

这一行。

这些点积是一种衡量两个向量相似度的方法。如果它们非常相似，点积就会很大。如果它们非常不同，点积就会很小，甚至是负数。

只让 query 使用过去的 keys，这个想法使它成为 `causal self-attention`，也就是因果 self-attention。换句话说，tokens 不能“看到未来”。

还有一个细节是，在做完点积后，我们会除以：

```text
sqrt(A)
```

其中 `A` 是 Q/K/V 向量的长度。这个缩放用于防止过大的值在下一步归一化，也就是 softmax 中占据主导。

我们会基本跳过 softmax 操作的细节，后面会描述它。这里只需要知道，每一行都会被归一化，使其总和为 1。

最后，我们可以为当前列：

```text
t = 5
```

生成输出向量。我们查看 `normalized self-attention matrix` 中第 `t = 5` 行，对其中每个元素，都用它去乘以其他列中对应的 V vector。

然后我们把这些结果相加，生成输出向量。因此，输出向量会主要由那些得分较高的列的 V vectors 主导。

现在我们知道了这个过程，就可以对所有列运行它。

这就是 self-attention 层中一个 head 的处理过程。因此，self-attention 的主要目标是：每一列都想从其他列中找到相关信息，并提取它们的 values。它通过把自己的 query vector 和其他列的 keys 比较来做到这一点，同时受到一个额外限制：它只能看过去。

## 6. Projection

在 self-attention 过程之后，我们得到了每个 head 的输出。这些输出是受 Q 和 K 向量影响后、被适当混合的 V vectors。

为了组合每个 head 的 `output vectors`，我们只是简单地把它们堆叠到一起。

例如，对于时间：

```text
t = 4
```

我们会从 3 个长度为：

```text
A = 16
```

的向量，变成 1 个长度为：

```text
C = 48
```

的向量。

值得注意的是，在 GPT 中，一个 head 内部的向量长度：

```text
A = 16
```

等于：

```text
C / num_heads
```

这确保了当我们把它们重新堆叠到一起时，会得到原来的长度：

```text
C
```

从这里开始，我们会执行 projection 来得到这一层的输出。这是在每一列上进行的一次简单矩阵-向量乘法，并加上 bias。

现在我们得到了 self-attention 层的输出。我们不会把这个输出直接传入下一阶段，而是把它按元素加回 input embedding。

这个过程在图中由绿色竖直箭头表示，称为 `residual connection`，也就是残差连接，或 `residual pathway`，残差路径。

和 layer normalization 一样，residual pathway 对于深度神经网络中的有效学习非常重要。

现在我们已经有了 self-attention 的结果，可以把它传入 Transformer 的下一部分：feed-forward network。

## 7. MLP

Transformer block 在 self-attention 之后的下一半是 MLP，也就是 `multi-layer perceptron`。这个名字有点绕口，但在这里，它只是一个包含两层的简单神经网络。

和 self-attention 一样，在向量进入 MLP 之前，我们会先执行一次 `layer normalization`。

在 MLP 中，我们会让每个长度为：

```text
C = 48
```

的列向量独立经过以下过程：

1. 一个带 bias 的线性变换，把它变成长度为：

```text
4 * C
```

的向量。

2. 一个 GELU 激活函数，逐元素应用。

3. 一个带 bias 的线性变换，把它投影回长度：

```text
C
```

现在，让我们跟踪其中一个向量。

我们首先执行带 bias 的矩阵-向量乘法，把这个向量扩展到长度：

```text
4 * C
```

注意，这里的输出矩阵在可视化中被转置了。这纯粹是为了可视化。

接下来，我们对向量中的每个元素应用 GELU 激活函数。这是任何神经网络中的关键部分，在这里我们为模型引入一些非线性。

这里使用的具体函数 GELU，看起来很像 ReLU 函数：

```text
max(0, x)
```

但它是一条平滑曲线，而不是一个尖锐的拐角。

然后，我们通过另一次带 bias 的矩阵-向量乘法，把向量投影回长度：

```text
C
```

和 self-attention + projection 部分一样，我们会把 MLP 的结果按元素加回它的输入。

现在，我们可以对输入中的所有列重复这个过程。

到这里，MLP 就完成了。现在我们得到了 Transformer block 的输出，它已经准备好传入下一个 block。

## 8. Transformer

这就是一个完整的 Transformer block。

这些 block 构成了任何 GPT 模型的主体，并会被重复多次。一个 block 的输出会被送入下一个 block，同时继续沿着 residual pathway 传递。

正如深度学习中常见的那样，很难准确说出每一层到底在做什么，但我们有一些大致想法：

较早的层倾向于学习较低层次的特征和模式，而较晚的层会学习识别和理解更高层次的抽象与关系。

在自然语言处理的语境中，较低层可能会学习语法、句法和简单的词语关联，而较高层可能会捕捉更复杂的语义关系、篇章结构和依赖上下文的含义。

## 9. Softmax

Softmax 操作会作为 self-attention 的一部分使用，就像我们在前一节看到的那样。它也会出现在模型的最后。

它的目标是接收一个向量，并把其中的值归一化，使它们的总和为 1.0。

不过，它并不是简单地除以总和。相反，每个输入值会先被指数化：

```text
a = exp(x_1)
```

这样做的效果是让所有值都变成正数。

一旦我们有了由指数化后的值组成的向量，就可以让每个值除以所有值的总和。这样就能确保所有值加起来等于 1.0。

因为所有指数化后的值都是正数，所以我们知道最终得到的值会在 0.0 和 1.0 之间。这为原始值提供了一个概率分布。

这就是 softmax：先指数化这些值，然后除以总和。

不过，这里有一个小问题。如果任何输入值非常大，那么指数化后的值也会非常大。我们最终可能会用一个大数除以一个非常大的数，这可能导致浮点运算问题。

Softmax 有一个有用的性质：如果我们给所有输入值都加上同一个常数，结果不会改变。

因此，我们可以找到输入向量中的最大值，并从所有值中减去它。这样可以确保最大值变成 0.0，同时 softmax 的结果仍然保持数值稳定。

让我们在 self-attention 层的语境中看看 softmax 操作。

每次 softmax 操作的输入向量，是 self-attention matrix 的一行，但只取到对角线为止。

和 layer normalization 一样，我们会有一个中间步骤，在那里存储一些 aggregation values，让计算过程更高效。

对于每一行，我们会存储这一行中的最大值，以及移位并指数化后的值的总和。

然后，为了生成对应的输出行，我们可以执行一小组操作：

```text
减去最大值
指数化
除以总和
```

为什么叫 softmax？

这个操作的“硬”版本叫做 argmax，它只是找到最大值，把它设为 1.0，并把所有其他值设为 0.0。

相比之下，softmax 是它的一个“更柔和”的版本。

由于 softmax 中涉及指数化，最大值会被强调并推向 1.0，同时仍然保持一个覆盖所有输入值的概率分布。

这允许模型不仅表示最可能的选项，也表示其他选项的相对可能性，从而得到更细腻的表达。

## 10. Output

最后，我们来到了模型的末端。

最终 Transformer block 的输出会先通过一个 layer normalization，然后我们会使用一个线性变换，也就是矩阵乘法。这一次没有 bias。

这个最终变换会把每一个列向量从长度 `C` 变成长度 `n_vocab`。

因此，它实际上是在为每一列中的词表里的每个词生成一个分数。

这些分数有一个专门的名字：`logits`。

`logits` 这个名字来自 `log-odds`，也就是每个 token 的 odds 的对数。

使用 `log` 是因为我们接下来应用的 softmax 会做指数化，把它们转换为 odds 或概率。

为了把这些分数转换成漂亮的概率，我们会把它们传入 softmax 操作。

现在，对于每一列，我们都有了模型为词表中每个词分配的概率。

在这个特定模型中，它实际上已经学会了三种字母排序问题的所有答案，所以这些概率会高度偏向正确答案。

当我们让模型按时间逐步前进时，会使用最后一列的概率来决定要添加到序列中的下一个 token。

例如，如果我们已经向模型提供了 6 个 token，我们就会使用第 6 列的输出概率。

这一列的输出是一系列概率，而我们实际上必须从中选出一个 token，作为序列中的下一个 token。

我们通过“从分布中采样”来完成这件事。也就是说，我们会按照每个 token 的概率权重，随机选择一个 token。

例如，一个概率为 0.9 的 token 会在 90% 的时间里被选中。

不过，这里也有其他选项，例如总是选择概率最高的 token。

我们还可以使用 temperature 参数来控制这个分布的“平滑程度”。

更高的 temperature 会让分布更加均匀，而更低的 temperature 会让分布更加集中在概率最高的 token 上。

我们是在应用 softmax 之前，先把 logits，也就是线性变换的输出，除以 temperature 来做到这一点的。

因为 softmax 中的指数化会对较大的数字产生很强的影响，所以让这些数字彼此更接近，会减弱这种影响。

