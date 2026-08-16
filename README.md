# yzu-comment

扬州大学教务系统「课堂教师」教学评估自动完成工具。

自动登录统一认证，获取评测任务列表，按预设规则作答（单选选最高分、多选全勾），填写主观评价并提交，无需人工逐门操作。

## 功能特性

- 自动登录统一认证（sso.yzu.edu.cn）
- 支持外网（WebVPN）与内网（校园网直连）两种访问方式
- 获取评测任务列表，区分待评估 / 已评估
- 自动作答：单选题选第一个选项（通常为 A_完全符合），多选题全部勾选
- 主观评价题填写 `config.json` 中的模板内容
- 演练模式（dry-run），先预览答案再提交
- 支持按教师姓名 / 课程名筛选
- 无头浏览器运行，无需桌面交互

## 工作原理

教务系统学生端通过统一认证平台（sso.yzu.edu.cn）登录，登录链路含 AES 加密与动态表单，难以用纯 HTTP 请求模拟。本工具基于 Playwright 驱动 Chromium：

1. 打开统一认证登录页，填入学号和密码完成登录
2. 进入教务系统，调用 `evaluation/queryAll` 接口获取评测任务
3. 逐门打开评测页，解析表单（单选 / 多选 / 主观评价）
4. 按规则构造答案，调用 `questionsAdd/doSave` 接口提交

## 环境要求

- Python 3.9+
- Windows / macOS / Linux

## 安装

```bash
git clone https://github.com/thanatosmoe/yzu-comment.git
cd yzu-comment

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
python -m playwright install chromium
```

## 配置

在项目根目录创建 `config.json`：

```json
{
  "comment_template": "老师教学认真负责，讲解清晰，课堂氛围良好，对课程内容掌握深入，授课方式灵活多样，能激发学生的学习兴趣，是一位非常优秀的老师。"
}
```

- `comment_template`：主观评价题的填写内容

## 使用

### 交互模式

```bash
python main.py
```

依次选择：访问方式（外网 / 内网）→ 输入学号和密码 → 选择操作（查看列表 / 自动评测 / 演练）。

### 命令行模式

```bash
python main.py list --net 1                 # 外网，查看评测任务列表
python main.py run --net 1                  # 外网，自动评测所有待评估任务
python main.py run --net 2                  # 内网，自动评测
python main.py run --net 1 --dry-run        # 演练，预览答案，不真正提交
python main.py run --net 1 -k 张三          # 只评测姓名或课程名包含“张三”的任务
python main.py run --net 1 --headed         # 显示浏览器窗口，遇验证码时手动处理
```

### 参数说明

| 参数 | 说明 |
| --- | --- |
| `--net 1` | 外网（WebVPN） |
| `--net 2` | 内网（校园网直连） |
| `--dry-run` | 演练模式，只预览不提交 |
| `-k 关键字` | 按教师姓名 / 课程名筛选 |
| `--headed` | 显示浏览器窗口（默认无头） |

## 作答规则

默认规则在 `main.py` 的 `URP.submit()` 中：

- 单选题：选择第一个选项（通常为 `A_完全符合`）
- 多选题（checkbox）：全部勾选
- 主观评价题：填写 `config.json` 中 `comment_template` 的内容

需要调整时修改 `payload` 构造逻辑即可。

## 目录结构

```
yzu-comment/
├── main.py            # 主程序
├── config.json        # 主观评价模板（不入库）
├── requirements.txt   # 依赖
├── README.md
└── .gitignore
```

## 使用提示

- 内网模式需处于校园网环境，否则使用外网模式
- 已评估过的任务不会重复提交
- 登录页若出现验证码，使用 `--headed` 手动完成
- 评测结果提交后不可撤销，正式提交前建议先跑一次 `--dry-run`

## 免责声明

本工具用于完成本人的教学评估操作，请在学校评测开放期内合规使用。使用者须自行遵守所在学校及教务系统的相关规定，因使用本工具产生的一切后果由使用者本人承担。本项目不提供任何形式的账号信息，也不参与任何评测数据造假。
