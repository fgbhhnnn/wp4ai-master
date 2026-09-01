# wp4ai

#### 介绍

Wordpress for AI

#### 安装教程

1.  安装python环境
2.  安装依赖

```bash
   pip install -r requirements.txt
```

#### 运行脚本

```bash
python3 wp4ai_generate.py
```

#### 注意事项

1. products目录的规范

- Baseball Jackets 【第一层为分类名】
  - Original Design ... 【第二层为产品名】
    - SKU-01-Black-1.jpg等 【第三层图片文件】

#### Yoast SEO 写入配置

Yoast SEO 的标题、描述和焦点关键词属于 WordPress 受保护 meta。首次使用新版程序前：

1. 在 WordPress 后台进入「插件 > 安装插件 > 上传插件」。
2. 上传发布包中的 `wp4ai-yoast-rest-meta.zip`。
3. 启用 `WP4AI Yoast REST Meta`，并保持 Yoast SEO 与 WooCommerce 已启用。

程序会写入 `_yoast_wpseo_focuskw`、`_yoast_wpseo_title` 和
`_yoast_wpseo_metadesc`。现有 `saswp_custom_schema_field`/JSON-LD 写入保持独立，
不会调用旧 SEO 插件的专用 REST 接口。
