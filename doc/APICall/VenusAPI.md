import os
import json
import requests

# 代理token请前往 [https://venus.woa.com/#/openapi/accountManage/personalAccount](https://venus.woa.com/#/openapi/accountManage/personalAccount)  查看代理token，并替换后调用

token = os.environ.get('ENV_VENUS_OPENAPI_SECRET_ID') + "@5172"

url = "[http://v2.open.venus.oa.com/llmproxy/chat/completions](http://v2.open.venus.oa.com/llmproxy/chat/completions)"

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

# 判断是否异常

if response.status_code != 200:
    # 异常处理
    print(response.json())
    exit()

print(response.json())