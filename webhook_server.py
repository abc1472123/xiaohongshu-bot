"""
飞书多维表格 HotAI 生图 Webhook 服务
触发链路：Base 自动化 → HTTP POST → 本服务 → HotAI 生图 → 回填图片到 Base
"""

import os
import time
import json
import base64
import tempfile
import requests
from flask import Flask, request, jsonify
from pathlib import Path

app = Flask(__name__)

# ==================== 配置区 ====================
FEISHU_APP_ID = "cli_aa91881961f89cc3"
FEISHU_APP_SECRET = "VS9ScmoQvEh62eTG2MI5DfsO7zhuht4N"
FEISHU_BASE_TOKEN = "J7Iob8zXFabUwAsz1WwcF1B3n1d"
FEISHU_TABLE_ID = "tblcCuFiSzN23NZy"
FEISHU_FIELD_ID_IMAGE = "fldPhqEiVt"

HOTAI_API_KEY = "sk-33359faed7c9b49110c35200112432d2536a8965223c89f24e1c42aaf1b69500"
HOTAI_API_URL = "https://www.hotaitool.net/v1/images/generations"
HOTAI_MODEL = "gpt-image-2"


def get_feishu_app_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    resp = requests.post(url, headers=headers, json=data, timeout=15)
    resp.raise_for_status()
    token = resp.json().get("tenant_access_token")
    if not token:
        raise Exception("获取飞书 token 失败")
    return token


def upload_image_to_feishu_drive(image_path, app_token):
    url = "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
    filename = os.path.basename(image_path)
    file_size = os.path.getsize(image_path)
    with open(image_path, "rb") as f:
        files = {"file": (filename, f, "image/png")}
        data = {
            "file_name": filename,
            "parent_type": "bitable_file",
            "parent_node": FEISHU_BASE_TOKEN,
            "size": str(file_size),
        }
        headers = {"Authorization": f"Bearer {app_token}"}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"上传飞书失败: {result}")
    return result["data"]["file_token"]


def upload_attachment_to_base_record(record_id, field_id, file_token, app_token):
    patch_url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_BASE_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )
    patch_data = {"fields": {field_id: [{"file_token": file_token}]}}
    patch_resp = requests.put(
        patch_url,
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        json=patch_data,
        timeout=15
    )
    patch_resp.raise_for_status()
    return patch_resp.json()


def generate_image_via_hotai(prompt):
    headers = {
        "Authorization": f"Bearer {HOTAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": HOTAI_MODEL, "prompt": prompt, "n": 1, "size": "1024x1024"}
    resp = requests.post(HOTAI_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    task_id = result.get("id")
    if task_id:
        status_url = f"https://www.hotaitool.net/v1/images/generations/{task_id}"
        for _ in range(24):
            time.sleep(5)
            status_resp = requests.get(status_url, headers=headers, timeout=10)
            status_result = status_resp.json()
            status = status_result.get("status", "")
            if status == "completed":
                data_list = status_result.get("data", [])
                if data_list:
                    b64_data = data_list[0].get("b64_json", "")
                    if b64_data:
                        return base64.b64decode(b64_data)
                    url_data = data_list[0].get("url", "")
                    if url_data:
                        img_resp = requests.get(url_data, timeout=30)
                        return img_resp.content
            elif status == "failed":
                raise Exception(f"HotAI 生图失败: {status_result}")
        raise Exception("HotAI 生图超时（>120s）")
    data_list = result.get("data", [])
    if data_list:
        b64_data = data_list[0].get("b64_json", "")
        if b64_data:
            return base64.b64decode(b64_data)
        url_data = data_list[0].get("url", "")
        if url_data:
            img_resp = requests.get(url_data, timeout=30)
            return img_resp.content
    raise Exception(f"HotAI 返回格式异常: {result}")


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_body = request.get_json(silent=True) or {}
        print(f"[收到请求] {json.dumps(raw_body, ensure_ascii=False)}")

        payload = raw_body
        if isinstance(payload, dict) and isinstance(payload.get("Body"), str):
            try:
                payload = json.loads(payload["Body"])
                print(f"[剥开 Body 包装] {payload}")
            except Exception:
                payload = {}
        elif isinstance(payload, dict) and isinstance(payload.get("body"), str):
            try:
                payload = json.loads(payload["body"])
                print(f"[剥开 body 包装] {payload}")
            except Exception:
                payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            record_data = {**payload, **payload["data"]}
        else:
            record_data = payload if isinstance(payload, dict) else {}

        def find(name_list):
            for n in name_list:
                v = record_data.get(n)
                if v not in (None, ""):
                    return v
            return None

        record_id = find(["record_id", "recordId"]) or ""
        keyword = find(["关键词", "keyword", "Keyword"]) or ""
        content = find(["仿写笔记内容", "content", "Content"]) or ""

        print(f"[解析结果] record_id={record_id}, keyword={keyword}, content_len={len(content)}")

        if not keyword:
            print(f"[错误] 没找到关键词，可见 keys: {list(record_data.keys())}")
            return jsonify({"code": 1, "msg": "未找到关键词", "keys": list(record_data.keys())}), 400

        prompt = (
            f"小红书爆款笔记封面图。关键词：{keyword}。笔记内容：{content}。风格：高质量、ins风、暖色调、有氛围感。"
            if content else
            f"小红书爆款笔记封面图。主题：{keyword}。风格：高质量、ins风、暖色调、有氛围感。"
        )

        print(f"[Step 1] 调用 HotAI 生图...")
        image_bytes = generate_image_via_hotai(prompt)
        print(f"[Step 1] 完成，{len(image_bytes)} bytes")

        tmp_path = tempfile.mktemp(suffix=".png")
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        print(f"[Step 2] 保存临时图片: {tmp_path}")

        app_token = get_feishu_app_token()
        print(f"[Step 3] 获取飞书 token 成功")

        file_token = upload_image_to_feishu_drive(tmp_path, app_token)
        print(f"[Step 4] 上传成功，file_token={file_token}")

        if record_id:
            upload_attachment_to_base_record(record_id, FEISHU_FIELD_ID_IMAGE, file_token, app_token)
            print(f"[Step 5] 回填成功！")

        os.remove(tmp_path)
        return jsonify({"code": 0, "msg": "生图成功", "file_token": file_token, "keyword": keyword})
    except Exception as e:
        print(f"[错误] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"code": 1, "msg": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
