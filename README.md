# LinuxDo Daily Check-in (GitHub Actions 版)

Fork 自 [doveppp/linuxdo-checkin](https://github.com/doveppp/linuxdo-checkin)，原样使用代码，**仅改动部署方式**：从本机 headless 改到 GitHub Actions（CF v3 challenge 阻挡所有 headless 浏览器自动化）。

## 每天运行

- 触发时间：**北京时间 18:00**（UTC 10:00）
- 运行环境：`ubuntu-latest`（GitHub 美国数据中心 IP，过 Cloudflare）
- 工作流：`.github/workflows/daily.yml`

## 配置

1. Fork 后到 **Settings → Secrets and variables → Actions**
2. **New repository secret**：
   - Name: `LINUXDO_COOKIES`
   - Value: 浏览器 F12 → Application → Cookies → `https://linux.do` → 全选复制

格式（DevTools 复制即可）：
```
_t=xxx; _forum_session=yyy; cf_clearance=zzz
```

## 手动测试

Actions tab → **Run workflow** 一次，验证配置通。

## 为什么不在 macmini 本机跑

Cloudflare 2026-05 之后升级到 v3 challenge，cf_clearance v3 格式（带 `--xxxx` 签名）阻挡：
- DrissionPage headless
- Playwright（加 stealth 也失败）
- Selenium / Puppeteer
- curl_cffi（被 403 拦）

**唯一可行路径是 GitHub Actions runner**（美国 IP，自动过 CF）。
