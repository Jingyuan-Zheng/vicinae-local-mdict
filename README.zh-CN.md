# Local MDict Dictionary for Vicinae

[English](README.md) | 简体中文

在 [Vicinae](https://vicinae.com) 内实时查询本地 MDX/MDD 词典。采用 React + TypeScript 原生列表和详情视图、独立 Python 后端，支持多词典分组、排序和选中文字快速查词。

## 实际截图

以下为本机 Linux/GNOME 上运行的扩展真实截图，仅裁剪应用窗口。词典内容版权归各自权利人；本项目不分发词典。

### 查词界面

![实际查询 ephemeral 的词典界面](docs/images/dictionary.png)

### 词典启用与排序

![实际扫描到的 MDX 文件及词典顺序](docs/images/configure-dictionaries.png)

## 功能

- 输入即搜索：精确、前缀、大小写兼容、Unicode、基础模糊匹配。
- 左侧按词典分组，右侧显示启用词典中所选词的释义。
- Configure Dictionaries 根据实际文件启用、禁用、排序及设置优先词典。
- 支持 MDX、同目录 MDD 和分卷 MDD，没有 MDD 的词典也可使用。
- 从 MDD 或同目录文件加载图片、CSS、字体和音频。
- 浅色/深色适配、持久索引和缓存，原始词典留在原处。
- Actions 提供复制单词、复制释义、发音、用 GoldenDict-ng 打开、重载及关联词查询。
- GNOME 可配置 `Super+Z` 选词查词。

## 环境与安装

本机基线为 **Linux/GNOME、Vicinae v0.28.1、SDK 0.22.3**（见 lockfile），其他版本未逐一验证。需要 Vicinae、Node.js 22+ / npm、Python 3.10+ / pip / venv。Ubuntu/Zorin 缺少 venv 时安装 `python3-venv`。可选音频播放器为 pw-play、paplay、mpv 或 ffplay；选词脚本需要 xclip 和 timeout。

```bash
git clone https://github.com/Jingyuan-Zheng/vicinae-local-mdict.git
cd vicinae-local-mdict
./install-local.sh
```

脚本运行 `npm ci`、官方 `vici build`，安装到 `${XDG_DATA_HOME:-$HOME/.local/share}/vicinae/extensions/local-mdict-dictionary`，在最终位置创建独立 `.venv`，不写入系统 Python。不要搬运虚拟环境，其脚本含绝对路径。可用 `PYTHON_BIN=/path/to/python3 ./install-local.sh` 指定解释器。

新增命令未出现时重启 Vicinae；`vicinae server --replace` 会结束当前会话并重新扫描。

## 词典目录和设置

默认目录为 **`~/Documents/Dictionaries`**，支持子目录。MDX 与配套 MDD 保持相同前缀、放在同目录：

```text
~/Documents/Dictionaries/
├── Oxford/
│   ├── Oxford.mdx
│   ├── Oxford.mdd
│   └── Oxford.1.mdd
└── Other/
    └── My Dictionary.mdx
```

在 Vicinae Settings → Local MDict Dictionary 修改 Dictionary Folder、Max Suggestions、Fuzzy Search。在 **Configure Dictionaries** 中按真实文件启用和排序，不需要输入词典名称。查询页下拉框可临时提升优先级。

## 连续阅读与选词查词

打开一次 Dictionary，用 `Super+Space` 隐藏和恢复；关闭 Vicinae 的 **Pop to root on close** 可保留页面。返回首页或重启应用会结束会话。

在 GNOME Settings → Keyboard → Custom Shortcuts 添加：

```text
名称：Lookup Selected Text in Dictionary
命令：/home/YOUR_USER/.local/share/vicinae/extensions/local-mdict-dictionary/assets/lookup-selected-text.sh
快捷键：Super+Z
```

替换用户名，使用 XDG_DATA_HOME 时调整路径，先检查快捷键冲突。安装脚本不自动修改桌面快捷键。桌面 PATH 找不到 Vicinae 时，可使用 `env VICINAE_BIN=/实际路径/vicinae /完整路径/lookup-selected-text.sh`。

脚本在窗口获取焦点前读取 X11/XWayland PRIMARY，成功后直接传入查询词；失败后回退官方 getSelectedText API，重试约 800 毫秒。不模拟 Ctrl+C，不读取普通剪贴板。原生 Wayland 能否取词取决于应用和合成器；获取不到时显示提示，API 入口拒绝超过 200 字符的选区。

## 渲染和缓存限制

释义通过 `List.Item.Detail` 的 rich-text 通道显示安全 HTML，其传输属性仍名为 `markdown`。这是 **Qt Rich Text，并非完整浏览器 WebView**。扩展内联和适配所支持的 HTML/CSS，不保证复杂网页布局、伪元素、动态交互或像素级还原。脚本、事件处理器、框架和远程资源会被移除。关联词通过 **Actions → Look Up Linked Word** 查询，原文点击不保证支持。

索引、渲染结果及按需提取资源会额外占用磁盘，位于 `environment.supportPath/mdict-cache`；不复制整本 MDX/MDD。首次初始化较慢，后续复用缓存。初始化核对文件签名，Reload Dictionaries 可手动重建；单本损坏词典隔离报错。清缓存前退出词典命令，下次查询会重新生成。

## 开发

```bash
npm ci
npm run typecheck
npm run lint
npm run build       # 安装到本地 Vicinae 扩展目录
./setup-helper.sh   # 可选：创建源码目录后端 venv
```

| 文件 | 用途 |
| --- | --- |
| `src/dictionary.tsx` | 查询界面、Actions、后台通信 |
| `src/configure-dictionaries.tsx` | 文件扫描、启用和排序 |
| `src/lookup-selected-text.tsx` | 官方选区读取与重试 |
| `assets/backend/helper.py` | MDict、缓存、资源、样式适配 |
| `assets/lookup-selected-text.sh` | 焦点切换前捕获选区 |
| `docs/images/` | 实际运行截图 |

## 许可证

代码采用 [MIT](LICENSE)。文件格式由 [mdict-mquery 2.1.0](https://pypi.org/project/mdict-mquery/) 处理，不自行实现 MDict。依赖保留各自许可证。

请自行合法取得词典。词典、字体、图片和录音版权归各自权利人；仓库不上传词典文件、用户缓存、虚拟环境、凭据或系统配置。截图展示本地词典运行效果，不代表授予词典再分发许可。
