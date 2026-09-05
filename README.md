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

Yoast SEO 的标题、描述和焦点关键词属于 WordPress 受保护 meta。若站点没有开放
Yoast `yoast/v1` REST 路由，请按以下步骤安装辅助插件：

1. 在 WordPress 后台进入「插件 > 安装插件 > 上传插件」。
2. 上传发布包中的 `wp4ai-yoast-rest-meta.zip`。
3. 启用 `WP4AI Yoast REST Meta`，并保持 Yoast SEO 与 WooCommerce 已启用。

程序会写入 `_yoast_wpseo_focuskw`、`_yoast_wpseo_title` 和
`_yoast_wpseo_metadesc`。现有 `saswp_custom_schema_field`/JSON-LD 写入保持独立，
不会调用其他 SEO 插件的专用 REST 接口。

程序每次运行前都会检查站点是否提供 Yoast `yoast/v1` REST 路由，或是否暴露上述
三个可写 meta 字段。两种方式都不可用时，程序会停止并提示安装辅助插件或开放
Yoast 路由，不会创建分类、上传图片或发布产品。

产品 SEO 提示词必须在 GUI 的「产品SEO提示词」文本框中填写。该内容会原样作为
`generate_seo_data_by_keywords` 的 system message，源代码不再内置产品提示词。
分类 SEO 提示词必须在 GUI 的「分类SEO提示词」文本框中填写。该内容会原样作为
`_generate_cat_seo` 的 system message，源代码不再内置分类提示词。两个提示词分别
保存为 `SEO_SYSTEM_PROMPT` 和 `SEO_CATEGORY_SYSTEM_PROMPT`，互不覆盖。
