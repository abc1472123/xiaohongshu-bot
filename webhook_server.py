"""
飞书多维表格 HotAI 生图 Webhook 服务（异步版 v2）
触发链路：Base 触发 → HTTP POST → 立即返回 200（不超时）→ 后台线程处理
设计要点：
  1. webhook 入口只解析参数 + 启动后台线程，立即返回
  2. 后台线程独立完成 HotAI 生图、上传飞书、回写 Base
  3. 失败详细打日志，HTTP 响应不再承担业务结果
"""
import os
import time
import json
import base64
import tempfile
import threading
import requests
from flask import Flask, request, jsonify
from pathlib import Path

app = Flask(__name__)

# ==================== 配置区 ====================
FEISHU_APP_ID = "cli_aaa265fd49f81be9"
FEISHU_APP_SECRET = "7HcGhPUcb0tF6FnPtvSjIdf5mnVNrDhQ"
FEISHU_BASE_TOKEN = "J7Iob8zXFabUwAsz1WwcF1B3n1d"
FEISHU_TABLE_ID = "tblcCuFiSzN23NZy"
FEISHU_FIELD_ID_IMAGE = "fldPhqEiVt"

HOTAI_API_KEY = os.environ.get("HOTAI_API_KEY")
if not HOTAI_API_KEY:
    raise RuntimeError("环境变量 HOTAI_API_KEY 未设置，请先在 Render Environment 配")

HOTAI_API_URL = "https://www.hotaitool.net/v1/images/generations"
HOTAI_MODEL = "gpt-image-2"


# ==================== 飞书 API 工具 ====================
def get_feishu_app_token():
    """获取飞书 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    resp.raise_for_status()
    token = resp.json().get("tenant_access_token")
    if not token:
        raise Exception("获取飞书 token 失败")
    return token


def upload_image_to_bitable_attachment(image_path: str, tenant_token: str, record_id: str) -> dict:
    """终极修复版：上传图片并关联多维表格"""
    import os
    filename = os.path.basename(image_path)
    file_size = os.path.getsize(image_path)
    
    # 彻底清理 Token，防止环境变量里有不可见空格
    base_token = str(FEISHU_BASE_TOKEN).strip()
    table_id = str(FEISHU_TABLE_ID).strip()
    field_id = str(FEISHU_FIELD_ID_IMAGE).strip()

    headers = {"Authorization": f"Bearer {tenant_token}"}
    
    # --- 第一步：上传附件到该多维表格 ---
    upload_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base_token}/attachments"
    
    print(f"DEBUG: 尝试上传到 {upload_url}")
    
    with open(image_path, "rb") as f:
        # 飞书对 files 的格式要求非常严格
        files = {
            'file': (filename, f, 'image/png'),
            'name': (None, filename)
        }
        upload_resp = requests.post(upload_url, headers=headers, files=files)
        
        if upload_resp.status_code != 200:
            print(f"❌ 上传失败详情: {upload_resp.text}")
            upload_resp.raise_for_status()
        
    file_token = upload_resp.json().get("data", {}).get("file_token")
    if not file_token:
        raise Exception(f"上传成功但未获取到 token: {upload_resp.text}")
        
    print(f"✅ 第一步成功：Token = {file_token}")

    # --- 第二步：回填到记录 ---
    update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}"
    update_data = {
        "fields": {
            field_id: [{"file_token": file_token}]
        }
    }
    
    update_resp = requests.patch(update_url, headers=headers, json=update_data)
    update_resp.raise_for_status()
    print(f"✅ 第二步成功：图片已写入记录！")
    
    return update_resp.json()
def upload_attachment_to_base_record(
    record_id: str, field_id: str, file_token: str, app_token: str
) -> dict:
    """把 file_token 写回 Base 记录的附件字段"""
    patch_url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_BASE_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )
    patch_data = {
        "fields": {field_id: [{"file_token": file_token}]}
    }
    patch_resp = requests.put(
        patch_url,
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        json=patch_data,
        timeout=15,
    )
    patch_resp.raise_for_status()
    return patch_resp.json()


# ==================== HotAI 生图 ====================
def generate_image_via_hotai(prompt: str) -> bytes:
    """调 HotAI 生图，返回图片二进制"""
    headers = {
        "Authorization": f"Bearer {HOTAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": HOTAI_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
    }
    resp = requests.post(HOTAI_API_URL, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    result = resp.json()
    task_id = result.get("id")
    if task_id:
        # 异步任务模式：轮询 task_id
        status_url = f"https://www.hotaitool.net/v1/images/generations/{task_id}"
        for _ in range(24):
            time.sleep(5)
            status_resp = requests.get(status_url, headers=headers, timeout=30)
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
    # 同步模式：直接返回
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


# ==================== 后台处理函数（核心异步逻辑）====================
def process_image_background(record_id: str, prompt: str):
    """后台线程：生图 → 上传飞书 → 回写 Base。失败详细打日志。"""
    tid = threading.current_thread().name
    try:
        print(f"[后台-{tid}] record_id={record_id} 开始处理")
        print(f"[后台-{tid}] prompt={prompt[:80]}...")

        # Step 1: HotAI 生图
        print(f"[后台-{tid}] [Step 1] 调用 HotAI 生图...")
        image_bytes = generate_image_via_hotai(prompt)
        print(f"[后台-{tid}] [Step 1] 生图完成，图片大小={len(image_bytes)} bytes")

        # Step 2: 存临时文件
        tmp_path = tempfile.mktemp(suffix=".png")
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        print(f"[后台-{tid}] [Step 2] 保存临时图片: {tmp_path}")

        # Step 3: 拿飞书 token
        print(f"[后台-{tid}] [Step 3] 获取飞书 App Token...")
        app_token = get_feishu_app_token()

        # Step 4: 上传图片到多维表格【生图】附件字段
        if record_id:
            print(f"[后台-{tid}] [Step 4] 上传图片到多维表格【生图】字段 record_id={record_id}...")
            upload_result = upload_image_to_bitable_attachment(tmp_path, app_token, record_id)
            print(f"[后台-{tid}] [Step 4] 上传到【生图】字段成功 result={upload_result}")
        else:
            print(f"[后台-{tid}] [Step 4] 缺少 record_id，无法上传到【生图】字段")
       
        # 清理临时文件
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        print(f"[后台-{tid}] ✅ record_id={record_id} 全部完成")

    except Exception as e:
        print(f"[后台-{tid}] ❌ record_id={record_id} 处理失败: {e}")
        import traceback
        traceback.print_exc()


# ==================== Flask Webhook（关键改造：立即返回 200）====================
@app.route("/webhook", methods=["POST"])
def webhook():
    """接收请求 → 解析参数 → 启动后台线程 → 立即返回 200（不等待生图）"""
    try:
        body = request.get_json()
        print(f"[收到请求] {json.dumps(body, ensure_ascii=False)}")
        record_data = body.get("data", {}) if isinstance(body.get("data"), dict) else {}

        def find_field(name):
            if name in record_data and record_data[name] not in (None, ""):
                return record_data[name]
            if name in body and body[name] not in (None, ""):
                return body[name]
            return None

        record_id = find_field("record_id") or find_field("recordId")
        keyword = find_field("关键词") or find_field("keyword") or ""
        content = find_field("仿写笔记内容") or find_field("content") or ""

        # 兼容 trigger_content
        if not keyword:
            trigger_content_str = body.get("trigger_content", "{}")
            try:
                trigger_content = (
                    json.loads(trigger_content_str)
                    if isinstance(trigger_content_str, str)
                    else trigger_content_str
                )
                if isinstance(trigger_content, dict):
                    keyword = (
                        trigger_content.get("关键词")
                        or trigger_content.get("keyword")
                        or ""
                    )
                    content = (
                        trigger_content.get("仿写笔记内容")
                        or trigger_content.get("content")
                        or content
                    )
            except Exception:
                pass

        if not keyword:
            return jsonify({"code": 1, "msg": "未找到关键词字段"}), 400

        # 拼 prompt（选项 B：仿写内容 + 关键词）
        prompt = (content + "\n关键词:" + keyword).strip() if content else keyword

        print(
            f"[生图] 关键词={keyword}, 仿写长度={len(content)}, record_id={record_id}"
        )
        print(f"[生图] prompt={prompt[:100]}...")

        # 关键：启动后台线程后立即返回（不阻塞 HTTP 响应）
        thread = threading.Thread(
            target=process_image_background,
            args=(record_id, prompt),
            daemon=True,
        )
        thread.start()

        return (
            jsonify(
                {
                    "code": 0,
                    "msg": "已接收，后台处理中",
                    "record_id": record_id,
                    "thread": thread.name,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"[错误] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"code": 1, "msg": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
