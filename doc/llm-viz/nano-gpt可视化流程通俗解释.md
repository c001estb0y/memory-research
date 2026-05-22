# nano-GPT 可视化流程通俗解释

本文基于 Brendan Bycroft 的 [LLM Visualization](https://bbycroft.net/llm) 和本地克隆的 `llm-viz` 源码，按网页左侧 walkthrough 的目录，解释截图中 nano-GPT 推理过程的含义。

这里的 `nano-gpt` 不是 ChatGPT 那种大模型，而是一个很小的 GPT 风格 Transformer 示例：

- 参数量约 85,584。
- 词表只有 3 个 token：`A`、`B`、`C`。
- 示例任务是“续写排序结果”：先把未排序字母 `CBABBC` 作为提示输入给模型，然后模型从它后面开始一个 token 一个 token 地续写排序后的字符，目标续写是 `ABBBCC`。所以完整序列可以理解为 `CBABBCABBBCC`，前半段是提示，后半段才是排序结果。
- 模型维度 `C = 48`，有 3 个 attention heads，每个 head 的向量长度 `A = 16`。
- 有 3 个 Transformer blocks。

可以把整个过程理解成一句话：

> GPT 每次不是直接“回答整段结果”，而是看已有 token，预测下一个 token 最可能是什么；预测出来后再把它接回输入，继续预测下一个。

## 先看懂画面

网页右侧的 3D 模型不是艺术图，而是把一次 GPT 推理过程拆成很多矩阵和向量。

- 蓝色块通常表示模型已经学好的权重，也就是训练后固定下来的参数。
- 绿色块通常表示正在计算的数字，也就是中间结果、激活值、概率等。
- 一列通常对应一个时间位置 `t`，也就是输入序列中的某个 token。
- 一列内部的高度通常对应向量维度 `C`，在 nano-GPT 中是 48。

如果你看到很多绿色竖条从上往下流动，可以理解为：输入 token 被一步步加工，最后变成“下一个 token 的概率”。

## 为什么输入是 CBABBC，输出却像 CBAAAA

截图里上方的 `Output` 行容易误解。它不是说模型已经把 `CBABBC` 一次性转换成了 `CBAAAA`，也不是最终排序结果。

它显示的是：对当前每个已输入位置，模型给出的“下一 token 预测”中概率最高的那个 token。

GPT 是自回归模型，意思是它按从左到右的方式预测下一个 token。对于长度为 6 的输入 `CBABBC`，模型其实同时算出了每个位置的输出概率：

```text
位置 1：看见 C             -> 预测下一个 token
位置 2：看见 C B           -> 预测下一个 token
位置 3：看见 C B A         -> 预测下一个 token
位置 4：看见 C B A B       -> 预测下一个 token
位置 5：看见 C B A B B     -> 预测下一个 token
位置 6：看见 C B A B B C   -> 预测下一个 token
```

网页真正用于继续生成的是最后一列，也就是“看完完整输入 `CBABBC` 后，下一个 token 最可能是什么”。截图里最后一列是粗体 `A`，意思是模型认为排序输出的第一个 token 很可能是 `A`。

下一步会把这个 `A` 接到输入后面：

```text
CBABBC -> CBABBCA
```

然后模型再跑一遍，用新的最后一列预测下一个 token。不断重复后，它才会逐步生成排序结果：

```text
CBABBC + ABBBCC
```

所以更准确地说：

> `CBABBC` 是提示部分，模型从它后面开始生成排序结果；`Output` 行里早期的 `C/B/A/...` 是各个前缀位置的预测展示，不是最终答案。

## 1. Introduction：输入到底是什么

例子里输入的是一串字母：

```text
CBABBC
```

对人来说这是字符，对模型来说不能直接处理字符。模型只认识数字，所以先把 token 映射成编号：

```text
A -> 0
B -> 1
C -> 2
```

因此 `CBABBC` 会先变成类似：

```text
2 1 0 1 1 2
```

但这只是最开始的整数编号。进入模型主体后，几乎所有计算都变成浮点数向量。

## 2. Preliminaries：这里讲的是推理，不是训练

这个网页展示的是 inference，也就是推理过程。

训练阶段已经结束了，蓝色权重已经学好。现在模型做的事情是：

1. 读取输入 token。
2. 使用已经训练好的权重进行大量矩阵计算。
3. 输出下一个 token 的概率分布。

所以你在可视化里看到的不是“模型如何学习”，而是“训练好的模型如何使用自己学到的权重来算答案”。

## 3. Embedding：把 token 变成向量

整数编号太单薄，比如 `B = 1` 并不表示 `B` 真的比 `A` 大。模型需要把每个 token 变成一个更丰富的向量。

Embedding 做两件事：

1. Token embedding：根据 token 是 `A/B/C`，查表得到一个长度为 `C = 48` 的向量。
2. Position embedding：根据它在序列里的位置，比如第 4 个 token，也查表得到一个长度为 48 的位置向量。

然后把两者相加：

```text
输入向量 = token embedding + position embedding
```

这一步的意义是：模型不仅知道“这个位置是 B”，还知道“这个 B 出现在第几个位置”。

最后，所有 token 都变成一列一列的 48 维向量，形成一个 `T x C` 的矩阵。`T` 表示时间位置，也就是序列长度；`C` 表示每个 token 的特征维度。

## 4. Layer Norm：把每一列的数值整理到稳定范围

Layer Normalization 可以理解成给每个 token 的向量做一次“数值整理”。

对某一列，也就是某一个 token 的 48 维向量，模型会计算：

- 平均值 mean。
- 标准差 standard deviation。
- 把这一列调整成平均值接近 0、标准差接近 1。
- 再乘上学到的权重 `gamma`，加上学到的偏置 `beta`。

直觉上，它的作用是让后续层拿到比较稳定的输入，避免某些数值突然过大或过小，让深层网络更容易工作。

## 5. Self Attention：让不同位置的 token 互相“看见”

Self Attention 是 Transformer 最核心的部分。

在 embedding 和 layer norm 里，每一列基本还可以独立处理。但到了 self-attention，每个位置会开始参考其他位置的信息。

它会为每个 token 生成三种向量：

- Q，Query：我现在想找什么信息。
- K，Key：我这里有什么信息可供别人匹配。
- V，Value：如果别人关注我，我能提供什么内容。

可以用查字典来类比：

```text
用 Query 去匹配一堆 Key，得到相关性分数；
再按这些分数，把对应的 Value 加权混合起来。
```

对于 GPT 来说还有一个关键限制：只能看当前位置和过去的位置，不能偷看未来。这叫 causal self-attention。

例如第 6 个 token 可以看第 1 到第 6 个 token，但不能看第 7 个 token。这样模型才能符合“从左到右预测下一个 token”的生成方式。

### Attention head 是什么

一个 attention head 可以理解成“一套独立的注意力观察方式”。

每个 head 都会自己生成 Q/K/V，自己计算“当前位置应该关注过去哪些位置”，然后得到一份自己的混合结果。多个 head 并行工作，意义是让模型可以同时从不同角度看同一段上下文。

比如在自然语言里，不同 head 可能分别关注：

- 某个词和它修饰对象之间的关系。
- 当前词和前面主语之间的关系。
- 标点、位置、重复模式或局部短语结构。

在这个 `A/B/C` 排序玩具模型里，head 不一定能直接被命名成某个清晰规则，但直觉类似：多个 head 可以并行捕捉不同位置关系、计数线索或排序线索。

nano-GPT 的主向量长度是 `C = 48`，有 3 个 heads，所以每个 head 处理 `A = 16` 维：

```text
head 1: 16 维
head 2: 16 维
head 3: 16 维
拼回去：16 + 16 + 16 = 48 维
```

### 用“我爱中华人民共和”理解 3 个 head

先强调一点：下面是帮助理解的类比，不是说真实模型里一定存在“第 1 个 head 专门做词组、第 2 个 head 专门做语法、第 3 个 head 专门做补全”的固定分工。真实 attention head 学到的是一组权重模式，功能可能混合、分散，也可能随层数变化。

假设输入是：

```text
我 爱 中 华 人 民 共 和
```

如果模型要预测下一个 token，很大概率应该预测：

```text
国
```

三个 attention heads 可以想象成三个同时工作的观察员：

**Head 1：看局部搭配**

这个 head 可能重点关注最后几个 token：

```text
人 民 共 和
```

因为“人民共和国”是很强的固定搭配，看到“共和”后，下一个字很可能是“国”。这个 head 提供的是局部短语补全线索。

**Head 2：看更长的实体边界**

这个 head 可能不只看“共和”，还会往前看：

```text
中 华 人 民 共 和
```

它捕捉到这不是普通的“共和”，而是“中华人民共和国”这个更完整的专名实体。这个 head 提供的是长程实体识别线索。

**Head 3：看句子语境和位置**

这个 head 可能关注更前面的上下文：

```text
我 爱 ...
```

“我爱 X”后面通常接一个名词性对象；而 `中华人民共和` 已经像一个国家名称的前缀，所以这个 head 会帮助模型判断：这里不是要接形容词、动词或标点，而是继续补完这个名词短语。

三个 head 的结果会被拼接起来，再经过 projection 混合。于是模型综合得到类似这样的判断：

```text
局部搭配：共和 -> 国
实体识别：中华人民共和 -> 中华人民共和国
句法语境：我爱 [国家名]
综合结论：下一个 token 很可能是“国”
```

如果只有一个 head，它也可以学到一些模式；但多个 head 的好处是允许模型并行保留多种线索。有的 head 看局部，有的 head 看远处，有的 head 看结构，有的 head 看重复或位置关系。最后 projection 再把这些线索融合回主向量。

## 6. Softmax：把分数变成概率

Attention 里会产生一排相关性分数，但这些分数还不是概率，可能有正有负，也不一定加起来等于 1。

Softmax 的作用是把一组分数变成概率分布：

```text
原始分数 -> 指数化 -> 除以总和 -> 概率
```

结果具有两个特点：

- 每个值都在 0 到 1 之间。
- 所有值加起来等于 1。

在 self-attention 里，softmax 表示“当前位置应该关注过去每个位置多少”。在最终输出里，softmax 表示“下一个 token 是 A/B/C 的概率分别是多少”。

## 7. Projection：把多个 head 的结果合回主通道

nano-GPT 有 3 个 attention heads。每个 head 都会独立做一套 Q/K/V attention。

每个 head 的输出向量长度是 `A = 16`，3 个 head 拼起来就是：

```text
16 + 16 + 16 = 48
```

也就是回到了主模型维度 `C = 48`。

Projection 做的事情是：

1. 把多个 head 的输出堆叠起来。
2. 再经过一次线性变换，也就是矩阵乘法加 bias。
3. 得到 self-attention 层的最终输出。

随后会做 residual connection，也就是把 attention 的结果加回原来的输入：

```text
新的向量 = 原输入向量 + attention 输出
```

这条“残差通路”很重要，它让深层模型更容易训练，也让信息可以一路往下传。

## 8. MLP：对每个位置单独做非线性加工

Self-attention 负责“不同 token 之间的信息混合”。MLP 则负责“对每个 token 自己的向量做进一步加工”。

在这个可视化中，MLP 大致是三步：

1. 线性变换：把长度 `C = 48` 的向量扩展到 `4C = 192`。
2. GELU 激活：给模型加入非线性能力。
3. 线性变换：再把长度 `192` 投影回 `48`。

然后再次做 residual connection：

```text
新的向量 = MLP 输入 + MLP 输出
```

如果没有 GELU 这类非线性，很多层矩阵乘法叠在一起本质上仍然接近一个大线性变换，表达能力会弱很多。

### Hidden layer 是什么

Hidden layer 可以直译为“隐藏层”。它不是神秘组件，而是指输入和输出之间的中间计算层。

在 MLP 里，`48 -> 192 -> 48` 中间那个 `192` 维空间就可以看作 hidden layer：

```text
输入层：48 维
隐藏层：192 维
输出层：48 维
```

为什么要先变宽再变回去？直觉上，模型先把信息展开到更大的中间空间里，经过 GELU 这样的非线性筛选、组合，再压回原来的 48 维。这个过程让模型不只是“搬运 attention 的结果”，还能对每个位置的特征做更复杂的变换。

## 9. Transformer：一个 block 的完整结构

一个 Transformer block 可以简化成：

```text
输入
  -> Layer Norm
  -> Self Attention
  -> Residual Add
  -> Layer Norm
  -> MLP
  -> Residual Add
输出
```

nano-GPT 有 3 个这样的 block。前一个 block 的输出会传给下一个 block。

Transformer block 可以理解成 GPT 里的一个“标准加工单元”。一个 block 内部通常包含两大能力：

- Self-attention：负责跨 token 取信息，让当前位置能参考过去位置。
- MLP：负责对当前位置的向量做更深的非线性加工。

多个 block 堆叠起来，就是让信息被反复整理、关联、加工。nano-GPT 只有 3 个 blocks，而 GPT-2 small 有 12 个 blocks，GPT-3 级别模型会更多。

直觉上：

- 前面的层可能学比较局部、简单的模式。
- 后面的层可能组合出更高层的关系。

在这个排序例子里，模型最终要学会：看到 `CBABBC` 这样的输入后，按某种生成规则输出排序后的字符。

## 10. Output：从最终向量变成下一个 token

经过所有 Transformer blocks 后，模型还会做最后一次 layer norm，然后用一个线性变换把每个位置的 48 维向量变成词表大小的分数。

因为这个例子的词表只有 3 个 token，所以每个位置会输出 3 个分数：

```text
A 的分数
B 的分数
C 的分数
```

这些分数叫 logits。logits 经过 softmax 后，就变成概率：

```text
P(A), P(B), P(C)
```

如果当前输入是 `CBABBC`，模型会看最后一列的输出概率，决定下一个 token。网页示例里，它会认为下一个很可能是 `A`。

之后可以把 `A` 接回输入，再跑一遍模型，继续预测下一个 token。不断重复，就能生成完整结果。

注意这里的“最后一列”很关键。模型虽然会为每个位置都算出概率分布，但生成时通常只取当前输入末尾位置的预测结果。截图里 `Output` 行显示多个位置，只是为了帮助你看到模型在每个前缀上的预测；真正用于下一步生成的是最后一个粗体位置。

## 一句话串起来

以 nano-GPT 为例，整个过程可以压缩成：

```text
字符
-> token 编号
-> token embedding + position embedding
-> 多层 Transformer block
   -> Layer Norm
   -> Self Attention：让当前位置参考过去位置
      -> 多个 attention heads 并行观察不同关系
   -> Projection + Residual
   -> Layer Norm
   -> MLP：48 -> 192 hidden layer -> 48
   -> Residual
-> logits
-> softmax 概率
-> 采样或选择下一个 token
```

如果只记一个核心理解：

> GPT 的本质是“上下文中的下一个 token 预测器”。Transformer 里的 embedding、attention、MLP、softmax，都是为了把已有上下文一步步变成“下一个 token 的概率分布”。

