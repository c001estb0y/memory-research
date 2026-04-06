import json
import requests
​
url = "http://api.timiai.woa.com/ai_api_manage/llmproxy/responses"  # 正式服：替换域名  api.timiai.woa.com
data_dict = {
    'model': 'gpt-5',  # 模型对应平台库可查询到的模型名称
    'input': [
        {
            "role": "user",
            "content": "Hello word"
        }
    ]
}
​
headers = {
    'Content-Type': 'application/json',
    'Authorization': '7HQXF******sPmYxMp'  # 对应平台个人API Key, 且确保API key有对应模型调用权限
}
​
response = requests.post(url, headers=headers, data=json.dumps(data_dict))
​
# 判断是否异常
if response.status_code != 200:
    # 异常处理
    print(response.json())
    exit()
​
print(response.json())






from openai import OpenAI

client = OpenAI(
    base_url="http://api.timiai.woa.com/ai_api_manage/llmproxy",   # 正式服替换域名  api.timiai.woa.com
    api_key='7HQXF******sPmYxMp'  # 对应平台个人API Key, 且确保API key有对应模型调用权限
)

# 使用 OpenAI Python 库发送请求
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
