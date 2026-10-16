#!/usr/bin/env sh
# 改了模板里的 class 之后运行，重新生成 static/app.css（需要 Tailwind v4 standalone 可执行文件 tailwindcss）
cd "$(dirname "$0")/.." && tailwindcss -i static/src.css -o static/app.css --minify
