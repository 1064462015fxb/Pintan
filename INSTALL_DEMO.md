# 品探 AI 智能上新助手 Demo 安装说明

本 Demo 包含品探的后端服务、前端工作台、1688 浏览器采集助手、Agent Skill、离线测试和示例数据。
压缩包不包含本机 `.env` 密钥、SQLite 数据库、商品图片、生成媒体、运行日志、模型缓存、虚拟环境或 Git 历史。

## 一、运行环境

- Windows 10/11
- Python 3.11（推荐使用 Miniconda/Anaconda）
- Node.js 18+（仅运行扩展与前端自动化测试时需要）
- Chrome 或 Edge 120+（需要使用 1688 浏览器采集时）
- 可访问 Qwen 兼容接口的 API Key（仅调用模型时需要）

图片抠图、OCR 和短视频相关依赖会占用较多磁盘空间。首次安装建议预留至少 5 GB；没有模型或图片任务时，可以先完成文字采集和离线测试。

## 二、创建 Python 环境

在项目根目录打开 Anaconda Prompt 或 PowerShell：

```powershell
conda create -n pintan python=3.11 -y
conda activate pintan
python -m pip install --upgrade pip
python -m pip install --no-build-isolation .
```

如果网络或编译环境导致安装失败，可先安装基础依赖，再重新执行最后一条命令。Windows 中文路径下不建议使用 editable 安装，以避免部分 Python 3.11 环境的路径编码问题。

## 三、配置本地模型参数

复制示例配置：

```powershell
Copy-Item .env.example .env
```

然后只在本机 `.env` 中填写真实 `QWEN_API_KEY`。密钥不要写入数据库、日志、前端、任务快照或 Git 仓库。其余模型名称和地址可按账号可用性调整。

## 四、初始化 Demo 数据并校验

```powershell
python -m pintan init-demo
python -m pintan validate
```

`init-demo` 会在本地 `data/pintan.db` 创建共享数据库并导入演示数据；该数据库属于运行时文件，不会被提交或打包。

## 五、启动工作台

```powershell
python -m pintan serve --port 8010
```

浏览器打开：<http://127.0.0.1:8010/>

常用页面：

- 数据采集：`/#collector`
- 批量选品：`/#selection/batch`
- 稳定选品：`/#selection/stable`
- 内容生产：`/#content`
- 铺货：`/#distribution`

## 六、加载 1688 浏览器采集助手（可选）

1. 打开 Chrome/Edge 的扩展管理页：`chrome://extensions` 或 `edge://extensions`。
2. 打开“开发者模式”。
3. 点击“加载解压缩的扩展”，选择本 Demo 的 `extension` 文件夹。
4. 在工作台生成配对码，在扩展弹窗中完成配对。
5. 保持扩展所在浏览器已登录 1688；工作台只接收页面证据，不读取或传输 Cookie、Token 或 LocalStorage。

批量选品第一阶段只读取标题、SKU、价格、销量、供应商和图片 URL 引用，不下载图片；选择商品进入内容生产后，才执行高清图片、OCR、分类和授权流程。

## 七、离线测试

```powershell
conda activate pintan
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_collector_extension.cjs
```

涉及真实 1688 页面、模型、OCR、抠图或 TTS 的测试需要额外的浏览器登录、API Key 和本地模型依赖，不能用离线 Demo 数据代替在线验收。

## 八、常见问题

### 页面打不开

确认服务进程仍在运行，并检查 8010 端口是否被占用：

```powershell
Get-NetTCPConnection -LocalPort 8010 -ErrorAction SilentlyContinue
```

### 模型调用失败

检查 `.env` 中的 `QWEN_API_KEY`、模型名和 Base URL；不要把密钥粘贴到前端或提交到仓库。

### 扩展显示未连接

在扩展管理页点击“重新加载”，确认 8010 服务正在运行，再回到工作台重新生成配对码。遇到登录或验证码时，先在扩展所在浏览器人工处理，再点击“继续采集”。

### 图片或视频任务很慢

图片生成、OCR、抠图和 TTS 属于独立耗时任务；任务页会保存阶段、成功数、失败数和错误原因。不要重复点击生成；必要时使用页面的中断或重试入口。

## 九、目录说明

```text
src/pintan/       后端、Agent、评分、素材与内容生产
src/pintan/static/前端工作台
extension/        Chrome/Edge 1688 采集助手
skills/           各 Agent 的业务边界与提示规范
tests/            离线回归测试
scripts/          导入、迁移、冒烟和性能脚本
docs/             数据映射、图片工作流和上架说明
```

