# Web GUI 子路径反向代理

Web GUI 可部署在 `/apps/probe/content/` 等子路径。HTTP API、JSON-RPC、
二进制流和浏览器会话均使用页面所在目录；状态栏端口由后端健康接口报告，
不会将请求改指代理无法访问的内部端口。

## Nginx 配置示例

后端仍在根路径监听，例如 `127.0.0.1:8765`。代理必须去掉挂载前缀、
转发 WebSocket Upgrade，并将**不带尾斜杠的入口重定向到带斜杠入口**。
Vite 使用相对资源地址；省略重定向会令浏览器从上一级目录请求脚本。

以下 `map` 放在 `http` 段中，两个 `location` 放在已有的 `server` 段中：

```nginx
map $http_upgrade $mklink_connection_upgrade {
    default upgrade;
    ''      close;
}

location = /apps/probe/content {
    return 308 /apps/probe/content/$is_args$args;
}

location /apps/probe/content/ {
    # 末尾 / 使 Nginx 用 / 替换匹配到的挂载前缀。
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $mklink_connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
```

访问 `/apps/probe/content?build=example#/dashboard?tab=superwatch` 时，
308 保留查询参数，浏览器继承原 fragment，最终从
`/apps/probe/content/?build=example#/dashboard?tab=superwatch` 加载。
`/apps/probe/content/index.html` 也支持。替换挂载前缀时，须同时修改
两个 location 和重定向目标，不能只修改其中一处。

使用其他代理时遵循相同约定：入口规范化、剥离前缀、保留查询参数、
转发 WebSocket、关闭 SSE 缓冲。后端无法从已经剥离前缀的请求中可靠推断
浏览器原始挂载入口，因此入口重定向属于代理配置。

## 验证

检查不带斜杠入口返回 308，带斜杠入口及 index.html 能加载 GUI，
健康接口返回实际监听端口，`ws/browser-session` 和 `ws/streams/*`
返回 101；关闭页面时释放请求应仍在挂载前缀内。必须在实际部署环境中
验证认证、TLS 和代理超时设置。现有根路径访问和 Tauri 侧车地址保持支持。

指令行为参见 [Nginx proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
和 [WebSocket 转发](https://nginx.org/en/docs/http/websocket.html)。
