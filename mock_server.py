from flask import Flask, request, jsonify

app = Flask(__name__)

# 通用的成功响应模板
def success_response(data=None):
    return jsonify({
        "code": 200, 
        "msg": "success", 
        "data": data or {},
        "access_token": "fake_token_123",  # 专门给登录用
        "token_type": "Bearer"
    })

# 1. 模拟登录接口
@app.route('/api/Token', methods=['POST'])
def login():
    print(f"[Mock] 收到登录请求: {request.json}")
    return success_response()

# 2. 模拟创建任务接口
@app.route('/api/AddTask', methods=['POST'])
def add_task():
    print(f"[Mock] 收到创建任务请求. 任务名称: {request.json.get('task_name')}")
    # 这里可以打印一下，帮你检查发过来的数据对不对
    return success_response({"task_id": 999})

# 3. 模拟获取资源/化学品列表 (防止其他地方报错)
@app.route('/api/v1/knowledge/getChemicalList', methods=['GET'])
def get_chem_list():
    return success_response({
        "chemical_list": [], 
        "chemical_sums": 0
    })

# 4. “万能”接口：只要是你没定义的接口，统统返回成功
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def catch_all(path):
    print(f"[Mock] 调用了通用接口: /{path}")
    return success_response()

if __name__ == '__main__':
    print("⚡ 模拟服务器已启动，地址: http://127.0.0.1:4669")
    app.run(port=4669)