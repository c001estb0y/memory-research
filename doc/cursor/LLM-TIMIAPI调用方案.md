# 内部 LLM API 代理网关调用方案

内部有两个 LLM API 代理网关可用，均支持 OpenAI Chat Completions 格式，按需选用：

| 代理网关 | 基础 URL | API 格式 | 鉴权方式 |
| --- | --- | --- | --- |
| **TIMI API** | `http://api.timiai.woa.com/ai_api_manage/llmproxy` | Anthropic Messages + OpenAI Chat Completions | 平台 API Key |
| **Venus API** | `http://v2.open.venus.oa.com/llmproxy` | OpenAI Chat Completions | 代理 Token（`SECRET_ID@5172`） |

---

## 零、Venus API（`/chat/completions`）

Venus 开放平台的 LLM 代理接口，使用 OpenAI Chat Completions 格式。

> 代理 Token 请前往 https://venus.woa.com/#/openapi/accountManage/personalAccount 查看并替换。

### 0.1 接口说明

| 项目 | 说明 |
| --- | --- |
| 接口类型 | POST |
| 数据格式 | JSON Body |
| 基础 URL | `http://v2.open.venus.oa.com/llmproxy/chat/completions` |

**Headers 参数**

| 参数名 | 必填 | 说明 |
| --- | --- | --- |
| Content-Type | 是 | 固定值：`application/json` |
| Authorization | 是 | `Bearer {SECRET_ID}@5172` |

### 0.2 Python 调用示例

```python
import os
import json
import requests

token = os.environ.get('ENV_VENUS_OPENAPI_SECRET_ID') + "@5172"

url = "http://v2.open.venus.oa.com/llmproxy/chat/completions"

payload = {
    'model': 'claude-sonnet-4-6',
    'messages': [
        {
            'role': 'system',
            'content': 'You are a helpful assistant.'
        },
        {
            'role': 'user',
            'content': 'Hello'
        }
    ]
}

headers = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {token}'
}

response = requests.post(url, headers=headers, data=json.dumps(payload))

if response.status_code != 200:
    print(response.json())
    exit()

print(response.json())
```

---

## 以下为 TIMI API 接口

TIMI API 支持两种 API 格式接入：

| API 格式 | 路径 | 适用模型 |
| --- | --- | --- |
| **Anthropic Messages API** | `/v1/messages` | Claude 系列 |
| **OpenAI Chat Completions API** | `/chat/completions` | 通用 LLM（GPT、Gemini 等） |

**正式服基础 URL**：`http://api.timiai.woa.com/ai_api_manage/llmproxy`

---

## 一、Anthropic Messages API（`/v1/messages`）

仅支持 Claude 系列模型的对话接口，支持文本、图像等多模态输入。

### 1.1 接口说明

| 项目 | 说明 |
| --- | --- |
| 接口类型 | POST |
| 数据格式 | JSON Body |
| Content-Type | `application/json` |

**Headers 参数**

| 参数名 | 必填 | 说明 |
| --- | --- | --- |
| Content-Type | 是 | 固定值：`application/json` |
| Authorization | 是 | 您的 API Key |

**Body 参数**

> 更多参数参考：https://platform.claude.com/docs/en/api/messages/create

| 参数 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| model | 是 | string | 模型名称，详见下方模型列表 |
| messages | 是 | array | 对话消息列表 |
| max_tokens | 是 | integer | 最大输出令牌数，最小值 1 |
| system | 否 | string/array | 系统提示词 |
| temperature | 否 | float | 随机性控制，范围 0.0-1.0，默认 1.0 |
| top_p | 否 | float | 核采样参数，范围 0-1 |
| top_k | 否 | integer | Top-K 采样，最小值 0 |
| stop_sequences | 否 | array | 自定义停止序列列表 |
| stream | 否 | boolean | 是否流式输出，默认 false |
| tools | 否 | array | 工具定义列表 |
| tool_choice | 否 | string/object | 工具选择策略：auto、any、none 或指定工具 |
| metadata | 否 | object | 请求元数据，如 user_id |

**支持的模型列表**（以平台为准）

| 模型名称 | 说明 |
| --- | --- |
| claude-opus-4-6 | 最智能模型，适合复杂任务和编码 |
| claude-sonnet-4-6 | 速度与智能的最佳平衡 |
| claude-haiku-4-5 | 最快模型，接近前沿智能 |
| claude-opus-4-5 | 高性能智能模型 |
| claude-sonnet-4-5 | 高性价比模型 |

**messages 参数说明**

messages 是一个消息对象数组，每个对象包含：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| role | 是 | string | 角色：`user` 或 `assistant` |
| content | 是 | string/array | 消息内容，可以是字符串或内容块数组 |

content 内容块类型：

| type | 说明 | 示例 |
| --- | --- | --- |
| text | 文本内容 | `{"type": "text", "text": "你好"}` |
| image | 图像内容（支持 base64） | `{"type": "image", "source": {...}}` |

**响应参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 消息唯一标识 |
| type | string | 对象类型，固定为 `message` |
| role | string | 角色，固定为 `assistant` |
| content | array | 生成的内容块数组 |
| model | string | 使用的模型名称 |
| stop_reason | string | 停止原因：`end_turn`、`max_tokens`、`stop_sequence`、`tool_use` |
| stop_sequence | string | 停止序列（如有） |
| usage | object | 令牌使用统计 |

### 1.2 Curl 调用示例

**基础调用**

```bash
curl -X POST 'http://api.timiai.woa.com/ai_api_manage/llmproxy/v1/messages' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: 7HQXF******sPmYxMp' \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "messages": [
      {
        "role": "user",
        "content": "Hello, Claude"
      }
    ],
    "system": "You are a helpful assistant."
  }'
```

**流式调用**

```bash
curl -X POST 'http://api.timiai.woa.com/ai_api_manage/llmproxy/v1/messages' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: 7HQXF******sPmYxMp' \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "stream": true,
    "messages": [
      {
        "role": "user",
        "content": "写一首关于春天的诗"
      }
    ]
  }'
```

### 1.3 Python 调用示例

**requests 格式请求**

```python
import json
import requests

url = "http://api.timiai.woa.com/ai_api_manage/llmproxy/v1/messages"

data_dict = {
    'model': 'claude-sonnet-4.6',
    'max_tokens': 1024,
    'messages': [
        {
            "role": "user",
            "content": "Hello, Claude"
        }
    ],
    'system': 'You are a helpful assistant.'
}

headers = {
    'Content-Type': 'application/json',
    'Authorization': '7HQXF******sPmYxMp'  # 替换为您的 API Key
}

response = requests.post(url, headers=headers, data=json.dumps(data_dict))

if response.status_code != 200:
    print(response.json())
    exit()

result = response.json()
print("AI 回复:", result['content'][0]['text'])
```

**使用 Anthropic Python SDK**

```python
from anthropic import Anthropic

API_KEY = "7HQXF******sPmYxMp"  # 您的 API Key
BASE_URL = "http://api.timiai.woa.com/ai_api_manage/llmproxy"

client = Anthropic(
    api_key='API_KEY',
    base_url=BASE_URL,
    default_headers={"Authorization": f"Bearer {API_KEY}"}
)

message = client.messages.create(
    model="claude-sonnet-4.6",
    max_tokens=1024,
    system="You are a helpful assistant.",
    messages=[
        {"role": "user", "content": "Hello, Claude"}
    ]
)

print("AI 回复:", message.content[0].text)
```

### 1.4 流式输出示例

```python
from anthropic import Anthropic

API_KEY = "7HQXF******sPmYxMp"  # 您的 API Key
BASE_URL = "http://api.timiai.woa.com/ai_api_manage/llmproxy"

client = Anthropic(
    api_key='API_KEY',
    base_url=BASE_URL,
    default_headers={"Authorization": f"Bearer {API_KEY}"}
)

kwargs = {
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "messages": [
        {"role": "user", "content": "你好"}
    ],
}

with client.messages.stream(**kwargs) as stream:
    for text in stream.text_stream:
        print("流回复：", text, end="\n", flush=True)
    print("完整回复:", stream.get_final_message().content[0].text)
```

### 1.5 多模态调用示例（图像分析，Base64 方式）

```python
import base64
import requests
import json

with open("image.jpg", "rb") as image_file:
    image_data = base64.b64encode(image_file.read()).decode('utf-8')

url = "http://api.timiai.woa.com/ai_api_manage/llmproxy/v1/messages"

data_dict = {
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "描述这张图片"
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_data
                    }
                }
            ]
        }
    ]
}

headers = {
    'Content-Type': 'application/json',
    'Authorization': '7HQXF******sPmYxMp',  # 替换为您的 API Key
}

response = requests.post(url, headers=headers, data=json.dumps(data_dict))
print(response.json())
```

---

## 二、OpenAI Chat Completions API（`/chat/completions`）

通用 LLM 调用接口，API 格式对齐 OpenAI，适用于所有 LLM 模型库中的模型。

### 2.1 接口说明

| 项目 | 说明 |
| --- | --- |
| 接口类型 | POST |
| 数据格式 | JSON Body |

**Headers 参数**

| 参数名 | 必填 | 说明 |
| --- | --- | --- |
| Content-Type | 是 | 固定值：`application/json` |
| Authorization | 是 | 平台个人 API Key |

**Body 参数**

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| model | 是 | 模型名称（平台模型库查看） |
| messages | 是 | 对话上下文 |
| stream | 否 | 是否开启流式输出，默认 true。非流式返回 JSON，流式回包格式对齐 OpenAI |

> 如无特别说明，LLM 模型库的模型调用方式相同，只需按需选择有权限的 model，修改 model 参数即可。

### 2.2 Curl 调用示例

```bash
curl -X POST 'http://api.timiai.woa.com/ai_api_manage/llmproxy/chat/completions' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: 7HQXF******sPmYxMp' \
  -d '{
    "model": "gpt-5",
    "messages": [
      {
        "role": "user",
        "content": "Hello"
      }
    ]
  }'
```

### 2.3 Python 调用示例

**requests 格式请求**

```python
import json
import requests

url = "http://api.timiai.woa.com/ai_api_manage/llmproxy/chat/completions"

data_dict = {
    'model': 'gpt-5',
    'messages': [
        {
            "role": "user",
            "content": "Hello word"
        }
    ]
}

headers = {
    'Content-Type': 'application/json',
    'Authorization': '7HQXF******sPmYxMp'  # 对应平台个人 API Key
}

response = requests.post(url, headers=headers, data=json.dumps(data_dict))

if response.status_code != 200:
    print(response.json())
    exit()

print(response.json())
```

**使用 OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://api.timiai.woa.com/ai_api_manage/llmproxy",
    api_key='7HQXF******sPmYxMp'  # 对应平台个人 API Key
)

response = client.chat.completions.create(
    model="gpt-5",
    messages=[
        {
            "role": "user",
            "content": "Hello word"
        }
    ]
)

print(response)
print("AI 回复:", response.choices[0].message.content)
```

### 2.4 多模态调用示例

```python
data_dict = {
    "model": "gpt-5",
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "描述这2张图"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://www.bing.com/rp/prF6k5Dpvr9a9EgM6ALGaqfZ-rw.jpg"
                    }
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://www.bing.com/rp/prF6k5Dpvr9a9EgM6ALGaqfZ-rw.jpg"
                    }
                }
            ]
        }
    ]
}
```

### 2.5 Google Search 联网搜索调用

> 仅支持 Gemini 系列 LLM 模型（`gemini-3.1-pro-preview-stb` 除外）

```python
data_dict = {
    "model": "gemini-pro",
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "今天微博头条新闻是什么"
                }
            ]
        }
    ],
    "web_search_options": {}  # 开启 Google Search 联网搜索
}
```
