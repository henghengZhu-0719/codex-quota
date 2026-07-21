# codex-quota

在终端或 macOS 菜单栏查询当前 Codex、Claude 登录账户的剩余额度。Codex 部分采用
与 OpenUsage 相同的方式：读取 Codex 已保存的 OAuth 登录，直接请求
`https://chatgpt.com/backend-api/wham/usage`，不需要启动 Codex App 或
`codex app-server`。

## 要求

- Python 3.9+
- 曾通过 Codex CLI 登录 ChatGPT 账户（打包后的应用运行时不依赖 `codex` 可执行文件）
- 菜单栏面板（`codex_quota_window.py`）仅支持 macOS，依赖 PyObjC
- 可选：已通过 `claude` 登录 Claude Code，用于显示 Claude Session/Weekly 剩余额度；
  未登录时面板里会显示对应的查询失败提示，不影响 Codex 部分正常工作

## 直接运行

```bash
python3 codex_quota.py
```

## 菜单栏面板

基于 `NSStatusItem` + `NSPopover` 原生实现（PyObjC/AppKit），不是第三方 GUI 框架
模拟出来的窗口，定位、圆角、阴影都由 macOS 系统直接处理（面板开合做成了瞬时
无动画，规避了脚本化启动场景下 `NSPopover` 关闭动画偶发卡死的问题，交互上更可靠）。

```bash
python3 -m pip install pyobjc-framework-Cocoa
python3 codex_quota_window.py
```

程序启动后常驻 macOS 顶部菜单栏，以“提供商图标 + 短周期剩余百分比”的形式并排
显示 Codex 与 Claude，例如 `Codex 图标 72%  Claude 图标 43%`；加载时显示 `—`，
查询失败时显示 `!`，并自动适配浅色/深色菜单栏。单击菜单栏图标即可在图标正下方
展开圆角深色详情面板（原生
popover，自带箭头指向图标），点击面板外部、按 Esc 或再次点击图标会自动收起；
右键图标可退出应用。Codex 与 Claude 的配额默认每 15 分钟一起自动刷新（这两个
窗口本身是 5 小时 / 7 天粒度，更高的频率没有意义，还会增加撞上接口限流的概率），
面板内也可手动刷新，也可以调整刷新间隔：

面板底部还会显示 Claude Session（5 小时窗口）、Weekly（7 天窗口），以及账户实际
返回的 Sonnet、Fable 和 Extra Usage，样式和 Codex 部分一致。Codex 的窗口会按返回的
实际周期识别，因此只有 Weekly 时也不会被误标成短周期。Anthropic 没有对应的公开
限额接口，
这里用的是 Claude Code 自己也在用的**非官方**接口 `GET api.anthropic.com/api/oauth/usage`：
按 Claude Code 的规则优先读取 macOS 钥匙串（包括 `CLAUDE_CONFIG_DIR` 对应的条目），
再退回读取配置目录中的 `.credentials.json`；某个来源失效时会继续尝试下一个来源。
OAuth access token 只用于向 Anthropic 发起这一次只读请求，不落盘、不外传、不做 token
刷新——所有来源都失效后，面板会提示重新运行 `claude` 登录，而不是静默续期。
这个接口没有文档、可能随时变化，请知悉。

每个提供商下方还会显示 **本地 Token 用量**，分别统计今天、昨天和最近 30 天，并用
一张可左右滑动的小柱状图展示最近 30 个自然日的每日用量（默认显示最新 7 天，
鼠标悬停到某根柱子时只显示当天的精确 Token 数）：

- Codex 扫描 `$CODEX_HOME`（默认 `~/.codex`）下的 `sessions/` 和
  `archived_sessions/` JSONL 日志；按累计值计算增量，并过滤重复快照、子代理回放和
  重复日志。
- Claude 扫描 `$CLAUDE_CONFIG_DIR/projects`（默认 `~/.claude/projects`）以及 Claude
  Cowork 的本地会话日志；按消息 ID 去重，避免 sidechain 回放重复计数。
- Token 统计全部在本机完成，只读取日志，不修改日志，也不会上传日志内容。已扫描且
  未变化的文件会在内存中复用解析结果，定时刷新不会反复解析全部历史记录。

```bash
python3 codex_quota_window.py --refresh 60
```

### HTTP 代理

点击面板右上角的 **⚙︎**，可以填写 HTTP(S) 代理，例如
`http://127.0.0.1:7890`。保存后会立即刷新，并同时用于 Codex 和 Claude 的用量
接口；点击 **清除** 后恢复系统默认网络设置。代理地址保存在当前用户的 macOS
应用偏好设置中，不会写入项目文件或日志。

终端命令也可以临时指定代理：

```bash
codex-quota --proxy http://127.0.0.1:7890
```

## 安装为命令

```bash
python3 -m pip install -e .
codex-quota
codex-quota-window
```

## 常用选项

```bash
# 输出原始 JSON
codex-quota --json

# 每 30 秒刷新
codex-quota --watch 30

# 禁用颜色
codex-quota --no-color

# 使用 HTTP 代理
codex-quota --proxy http://127.0.0.1:7890
```

运行 `codex-quota --help` 查看全部选项。

## 打包 macOS App

将应用图标放到 `assets/codex.icns` 或项目根目录的 `codex.icns`（没有图标时也可以
构建，只是使用默认图标），
然后运行：

```bash
python3 -m pip install -e '.[build]'
pyinstaller --noconfirm --clean CodexQuota.spec
```

生成的应用位于 `dist/CodexQuota.app`。构建配置已加入 `LSUIElement`，因此应用只在
顶部菜单栏常驻，不会同时占用 Dock。入口是 `codex_quota_window.py`，不是 `main.py`。

`.spec` 里已经为 `AppKit`、`Foundation`、`objc`、`PyObjCTools.AppHelper` 显式加了
`hiddenimports`；PyInstaller 打包 PyObjC 应用偶尔仍需按报错信息追加隐藏依赖，构建
完成后请实际打开 `dist/CodexQuota.app` 验证菜单栏图标和面板能正常工作。

## 说明

- “剩余百分比”按 Codex 返回的 `usedPercent` 计算。
- 重置时间会转换为本机时区，并显示距离重置的大致时长。
- Codex 用量接口属于 ChatGPT 后端接口，应用会在 OAuth Token 即将过期或接口返回
  401/403 时刷新 Token，并把轮换后的 Token 写回原来的 `auth.json` 或钥匙串。
- Claude 用量查询的接口/凭据读取方式参考了开源项目
  [robinebers/openusage](https://github.com/robinebers/openusage)（`docs/providers/claude.md`）
  的公开实现；同样属于非官方接口，Anthropic 调整后可能需要同步更新。
