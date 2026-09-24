# Global Dossier 国外局实审分析助手

输入中国专利公开号 → 直连 USPTO Global Dossier 公开接口 → 抓取 US/EP/JP/KR 国外同族历次实审通知书 → OCR（本地 rapidocr / MinerU 云端）→ 大模型自动生成《国外局实审过程分析报告》，页面展示并支持 Markdown / PDF 下载。

> 本仓库是 Flask Web 应用，**代码托管在 GitHub，程序需要在本机或自己的服务器运行**（GitHub Pages 只能托管纯静态页面）。

## 文件说明

| 文件 | 作用 |
|---|---|
| `gd_app.py` | Web UI（Flask，默认 http://127.0.0.1:7860） |
| `gd_helper.py` | 核心流水线：Global Dossier 抓取、同族解析、报告生成调度 |
| `extract.py` | 通知书文本结构化提取（对比文件/权利要求/审查结论） |
| `ocr.py` | OCR 双后端：本地 rapidocr / MinerU 云端 API |
| `gd_log.py` | 日志与过期缓存清理 |
| `.env.example` | 配置模板（复制为 `.env` 使用） |

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 配置密钥（重要！）
cp .env.example .env        # Windows: copy .env.example .env
# 编辑 .env，填入你自己的 LLM_API_KEY（大模型报告生成必需）
# OCR 默认用本地 rapidocr，无需额外配置；如需高精度可填 MINERU_API_KEY

# 3. 启动
python gd_app.py            # 自动打开浏览器 http://127.0.0.1:7860
```

常用参数：

```bash
python gd_app.py --port 9000 --no-browser        # 换端口、不自动开浏览器
python gd_app.py --host 0.0.0.0 --password xxx   # 公网部署 + Basic Auth 密码保护（用户名 gd）
```

## 密钥安全设计

- 代码中**不包含任何 API 密钥**（已脱敏为占位）。
- 密钥读取优先级：页面填写 > 环境变量（`LLM_API_KEY` 等）> 项目根 `.env`。
- `.env`、`peizhi.txt`（MinerU token）均已在 `.gitignore` 中，永远不会被提交到仓库。

## 公网部署提示

若部署到服务器供他人使用，务必：

1. 设置访问密码（`--password` 或环境变量 `GD_PASSWORD`），否则任何人可消耗你的大模型额度；
2. 建议前置 Nginx 加 HTTPS。

## 数据接口说明

程序直连 USPTO Global Dossier 公开 JSON 接口（CloudFront 域名见 `gd_helper.py` 的 `HOST` 常量），仅使用标准 GET 请求，无需注册、无浏览器依赖（TLS 指纹由 curl_cffi 模拟）。
