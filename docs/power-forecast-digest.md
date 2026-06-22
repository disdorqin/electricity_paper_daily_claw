# 电力预测科研日报企业微信推送

本功能每天自动抓取并总结电力预测/时序预测方向的最新论文和 GitHub 仓库，然后通过企业微信机器人推送到群聊。

## 关键词范围

- time series forecasting
- electricity load forecasting
- power load forecasting
- peak demand forecasting
- probabilistic forecasting
- model ensemble
- stacking / blending
- PatchTST / iTransformer / Mamba / TCN / LSTM / TFT

## 文件

- `.github/workflows/power-forecast-digest.yml`：每天北京时间 09:00 自动运行，也支持手动触发。
- `scripts/power_forecast_digest.py`：抓取 arXiv/GitHub，生成文章并推送企业微信。

## GitHub Secrets

进入仓库：

`Settings -> Secrets and variables -> Actions -> New repository secret`

需要配置：

| Secret | 是否必须 | 用途 |
|---|---:|---|
| `WECHAT_WEBHOOK` | 必须 | 企业微信机器人 webhook |
| `OPENAI_API_KEY` | 推荐 | 生成公众号风格总结；缺失时使用 fallback 模板 |
| `OPENAI_MODEL` | 可选 | 默认 `gpt-4o-mini` |

不要把 webhook、API key、GitHub token 写入代码、README、issue、PR 或聊天记录。

## 企业微信 webhook 获取

企业微信群 -> 右上角群设置 -> 群机器人 -> 添加机器人 -> 复制 Webhook。

## 手动测试

进入仓库 `Actions` 页面，选择 `Power Forecast Digest`，点击 `Run workflow`。

## 常见问题

### Missing WECHAT_WEBHOOK secret

说明没有配置 `WECHAT_WEBHOOK`，或 secret 名称拼错。

### 企业微信返回 invalid webhook / 400

检查 webhook 是否完整，是否来自当前企业微信群机器人，是否误删或重新生成。

### OPENAI_API_KEY 缺失

脚本会使用 fallback 模板，不会停止。但是总结质量会弱一些。

### OpenAI 429 / quota / billing

API 和 ChatGPT Plus 是分开计费的。确认 OpenAI Platform 已开通 API billing，并且 key 有额度。

### GitHub API rate limit

workflow 使用 `${{ github.token }}` 查询 GitHub Search API，通常够用。如果频率很高，可以改为专用 token，但不要把 token 写进代码。
