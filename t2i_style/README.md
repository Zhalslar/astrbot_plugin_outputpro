# notebook-maple

用于 `pillowmd.LoadMarkdownStyles()` 的仿真笔记本样式。它把 Markdown 渲染成一页干净的浅米色横线笔记纸，带红色边距线、左侧装订孔、细纸张阴影和偏工作笔记的排版节奏。

## 风格特点

- 适合普通聊天记录、说明文档、教程、清单、代码解释和长文本回复。
- 背景是低饱和浅纸色，不使用默认深蓝底，整体更清爽。
- 正文、标题、表格和代码块都优先保证清晰度，避免花纹干扰阅读。
- 代码块使用深色底，和正文纸张形成明显层级。
- 横线和装订孔只作为轻装饰，目标是像真实课堂/会议笔记而不是花哨主题。

## 字体

字体优先使用 `fonts/MapleMono-NF-CN-Regular.ttf` 和 `fonts/MapleMono-NF-CN-SemiBold.ttf`，中文、英文、数字和代码都走同一套清晰字体。

备用字体包括 `Symbola_hint.ttf`、`DroidSansFallbackFull.ttf`、`DejaVuSansMono.ttf`。其中 `Symbola_hint.ttf` 用来覆盖常见 emoji，避免显示成方框。不要改成 `.ttc` 字体集合，当前 pillowmd/fontTools 加载 `.ttc` 时可能需要字体编号，容易失败。

## 使用方式

在 AstrBot WebUI 的本插件配置里，把 `style_path` 填成：

```text
/AstrBot/data/plugins/astrbot_plugin_nobrowser_markdown_to_pic/styles/modern-clear
```

注意填目录，不要填 `setting.yml` 文件本身。

## 预览

预览图会生成在本目录的 `preview-notebook.png`，emoji 测试图会生成在 `preview-emoji.png`。
