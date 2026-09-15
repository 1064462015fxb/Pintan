# 品探 Agent 工作台

## 1688链接自动采集（助手2.0）

在 conda `pintan` 中运行 `python -m pintan serve --port 8010`，打开 `http://127.0.0.1:8010/#collector`。

批量选品入口为 `http://127.0.0.1:8010/#selection/batch`。先在扩展页重新加载
`extension/` 中的 2.4.0 版本并保持连接，输入“家居归纳”后系统会建议规范关键词
“家居收纳”。批量阶段只保存文字、SKU、价格、供应商字段及图片URL引用；只有在
稳定池勾选商品并点击“完善素材并创建内容”后才下载、OCR和分类图片。
工作台默认仅填商品链接，手动资料导入收起为备用。先在Chrome/Edge 120+加载项目 `extension` 目录，
在工作台生成配对码并到助手弹窗完成一次配对；然后点击“开始采集”。详情见 [扩展安装说明](extension/README.md)。

助手后台自动复用相同商品标签或新开详情页，采集文字、SKU和区域图片。后端保存证据后自动下载图片。
弹窗可关闭；遇到登录或验证码时到Chrome/Edge人工处理，再回工作台点“继续采集”。不能复用Codex内置浏览器登录状态。
结果分开显示本次采集与历史素材；零图片/区域不完整为“部分完成”，没有资料为失败；下载不自动授予图片使用权。

服务使用单worker、本机8010端口。配对令牌只在扩展本地，数据库只存哈希；不采集Cookie或请求头。
Windows中文路径建议使用常规安装 `python -m pip install . --no-deps --no-build-isolation --force-reinstall`，避免Python3.11的editable路径编码问题。
相关离线回归：`python -m unittest discover -s tests -p "test_collector*.py" -v`，`node --test tests/test_collector_extension.cjs`。
当前验证与未完成的真实页面验收见 [验证记录](outputs/collector-bridge-verification-20260911.md)。

系统以 `data/pintan.db` 为共享主库：选品 Agent 负责稳定评分和爆款匹配；内容母版 Agent 基于可追溯事实生成每个商品独立的图文与短视频母版。三张 CSV 仅用于首次导入和显式兼容导出，不再是运行时主库。

```powershell
python -m pintan init-demo   # 幂等建库并首次导入
python -m pintan export-csv  # 需要时显式导出兼容CSV
python -m pintan serve --port 8010
```

演示素材均由已有字段确定性推导并标记待复核。图片表只创建主图、功能图、参数图和场景图的“待采集”记录，不伪造图片、OCR、授权或详情链接。

## 阅读顺序

1. `pintan_market_trending_products.csv`：爆款需求和痛点。
2. `src/pintan/demo_data.py`：三表演示数据生成。
3. `src/pintan/stable.py`：稳定五维规则。
4. 三个 `skills/*/SKILL.md`：Agent 业务边界。
5. `src/pintan/agents.py`：Deep Agents 工厂、受限工具和稳定流程。
6. `src/pintan/trending.py`：爆款精简输入、缓存、工具总分、按需详解和审计。
7. `src/pintan/database.py` 与 `src/pintan/content_master.py`：共享数据库、事实血缘和母版版本。
8. `src/pintan/webapp.py` 与 `src/pintan/static/app.js`：调试接口、去重与页面。

## 爆款快速匹配

```powershell
python -m pintan trending --trend-id TR-DY-001
python -m pintan trending --trend-id TR-DY-001 --force
```

默认相同输入复用成功缓存。`--force` 或页面“强制重算”才重新调用模型；模型仍是 `qwen3.7-plus`，没有为提速自动换模型。

首轮十条记录只生成短理由及证据，`match_score` 完全由本地十进制计算工具生成（价格40%、特征25%、痛点25%、供应商10%），模型不能提交或覆盖总分。页面“展开 Top 3 详解”单独调用模型，不改变排名和评分。

缓存键包含实际使用的事实、候选集合、模型配置、Skill 内容和算法/提示词版本，不包含密钥、历史评分及采集时间等无关元数据。候选行换序不失效；事实、模型或版本变化会失效。缓存命中显示原生成时间和本次读取耗时。强制重算产生新的运行编号，使旧详解失效。

成功缓存、完整输入快照、运行结果和按需解释分别保存在 `outputs/match_cache`、`outputs/match_runs`、`outputs/match_details`；没有增加业务表。详解失败不会删除已有评分。页面重启后旧任务返回404，前端停止轮询，不会反复请求无效任务。

调试接口：`POST /api/tasks/trending/{trend_id}?force=true` 强制匹配，`POST /api/tasks/trending/{trend_id}/explanations` 生成详解；`GET /api/trends/{trend_id}` 返回结果、缓存时间与详解。排队和运行中的同一请求复用任务编号，不同请求冲突返回409。

## 性能实测（同样本，各三次）

固定 `TR-DY-001` 和十条候选，优化前后均为 `qwen3.7-plus`，各三次无缓存调用全部成功：中位耗时从 **87.843秒降至40.390秒（缩短54.02%）**；模型调用中位数由3次降至2次，纠错从1次降至0次。输入token中位数17572→6092，输出token中位数4960→2248。

相同输入缓存读取约0.015秒、模型调用0次；按需三条详解约15.2秒，评分表未变化。结果属于本机同一演示样本的顺序测试，不保证其他样本和服务负载下同样耗时。

原始测量与汇总在 `outputs/perf_v3/`。`scripts/benchmark_matching.py` 使用独立数据副本，`scripts/summarize_benchmark.py` 汇总测量。不要覆盖已有基准目录；重新测试时使用新输出目录。

## 安装与启动

当前已在 conda `pintan` 环境安装并验证依赖。请在项目目录运行：

```powershell
conda activate pintan
python -m pintan validate
python -m pintan serve --port 8010
```

本地密钥放在 `.env`（已被版本控制忽略），不是 `.venv`。已有配置时不要用示例覆盖。
普通安装采用 `python -m pip install --no-build-isolation .`；如修改源码，重新安装后重启服务。

从空环境初始化时：

```powershell
python -m pip install hatchling editables
python -m pip install --no-build-isolation .
Copy-Item .env.example .env
python -m pintan init-demo
python -m pintan validate
python -m pintan stable --no-model
python -m pintan serve
```

只在本地 `.env` 填写轮换后的 `QWEN_API_KEY`。默认稳定汇报使用 `qwen3.6-flash`，爆款匹配使用 `qwen3.7-plus`。打开 `http://127.0.0.1:8000`。单个爆款可运行 `python -m pintan trending --trend-id TR-DY-001`。

`init-demo` 检测到目标表时拒绝覆盖。搜索数据来自既有演示候选的确定性派生，并非 1688 实时采集；`offer_url`、`collected_at` 留空以避免冒充实采。稳定参考售价采用“到手成本 × 2”演示假设；竞争、差异化只代表当前候选池；信任分是代理指标；毛利未扣除广告、退款、平台费和税费。

测试：`$env:PYTHONPATH="src"; python -m unittest discover -s tests -v`。

前端网络与轮询回归：`node tests/test_frontend.cjs`。

## 图文与15秒视频

平台文案生成后，页面会出现“素材与成品”步骤：先从真实1688详情链接采集图片；若登录或反爬使采集失败，可人工上传 JPEG、PNG 或 WEBP。系统校验大小、像素、MIME 和 SHA-256，执行待复核 OCR 与主体抠图。只有用户逐图确认“拥有使用权”后，才允许调用 `qwen-image-2.0`。

三张成图分别是主图、场景图和卖点图。模型只画无商品、无文字的背景，真实商品透明抠图与已审核文字由 Pillow 合成。随后用三张图各展示5秒生成15秒 H.264 视频，并把平台文案中的三段原文烧录到画面；TTS配置可用时添加配音，失败时明确降级成静音字幕版。文件保存在 `data/media/`，SQLite只保存来源哈希、授权、模型、版本和人工确认状态。

关键配置见 `.env.example`：`QWEN_IMAGE_MODEL`、`QWEN_IMAGE_BASE_URL`、`QWEN_TTS_MODEL`、`QWEN_TTS_BASE_URL` 和 `FFMPEG_PATH`。本机使用 `imageio-ffmpeg` 提供固定FFmpeg二进制，避免依赖系统PATH。媒体接口只按数据库ID返回 `data/media/` 下的文件，任意路径访问会被拒绝。
