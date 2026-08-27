// 前端静态文件服务器 —— 用 Node 内置 http 模块托管 frontend/ 目录。
//
// 用法: node static-server.js <frontend-dir> <port>
//
// 端口注入: 前端 bundle 原本硬编码 WS=8080 / CAM=8081;当用户显式设了
// OPENSIM_WS_PORT=8081 或被占用被错开时,WS/CAM 会落到其他端口。
// 这里读 WS_PORT / CAM_HTTP_PORT 环境变量,在 HTML 里以 <script> 注入
// window.__OPENSIM__={wsPort,camPort},前端从该对象读取并回落到 8080/8081。
//
// 同时支持 CAM_BASE_URL(完整 URL,如 http://host:8081,跨机器访问时设)。
const http = require('http');
const fs = require('fs');
const path = require('path');

const root = process.argv[2] || './frontend';
const port = parseInt(process.argv[3] || '3000', 10);

const wsPort = parseInt(process.env.WS_PORT || '8080', 10);
const camPort = parseInt(process.env.CAM_HTTP_PORT || '8081', 10);
const camWsPort = parseInt(process.env.CAM_WS_PORT || '8082', 10);
const camBaseUrlEnv = process.env.CAM_BASE_URL || '';

const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png':  'image/png', '.jpg':  'image/jpeg', '.svg': 'image/svg+xml',
    '.map':  'application/json', '.woff2': 'font/woff2',
};

const rootResolved = path.resolve(root);

// 注入到 index.html 的 <head> 末尾。前端在 WS/CAM URL 构造处读这个对象。
// 注意: 必须是合法 JSON 子集,避免前端 parse 失败。
const injectScript = `<script>(function(){
window.__OPENSIM__ = window.__OPENSIM__ || {};
window.__OPENSIM__.wsPort = ${JSON.stringify(wsPort)};
window.__OPENSIM__.camPort = ${JSON.stringify(camPort)};
window.__OPENSIM__.camWsPort = ${JSON.stringify(camWsPort)};
window.__OPENSIM__.camBaseUrl = ${JSON.stringify(camBaseUrlEnv)};
})();</script>`;

const server = http.createServer((req, res) => {
    let urlPath = decodeURIComponent(req.url.split('?')[0]);
    if (urlPath === '/') urlPath = '/index.html';
    const filePath = path.join(rootResolved, urlPath);
    // 防目录穿越
    if (!filePath.startsWith(rootResolved)) {
        res.writeHead(403); res.end('Forbidden'); return;
    }
    fs.readFile(filePath, (err, data) => {
        if (err) {
            // SPA 回退到 index.html(处理前端路由)
            fs.readFile(path.join(rootResolved, 'index.html'), (e2, d2) => {
                if (e2) { res.writeHead(404); res.end('Not Found'); return; }
                sendHtml(d2, res);
            });
            return;
        }
        const ext = path.extname(filePath).toLowerCase();
        if (ext === '.html') {
            sendHtml(data, res);
            return;
        }
        res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
        res.end(data);
    });
});

function sendHtml(htmlBuf, res) {
    // html 是 Buffer,先转字符串才能 replace,再回写为 Buffer。
    const html = htmlBuf.toString('utf-8');
    const tag = '</head>';
    let out;
    if (html.includes(tag)) {
        out = html.replace(tag, injectScript + tag);
    } else {
        out = html + injectScript;
    }
    res.writeHead(200, { 'Content-Type': MIME['.html'] });
    res.end(out);
}

server.listen(port, () => {
    console.log(`Static server: ${rootResolved} on :${port}`);
    console.log(`  Injected __OPENSIM__.wsPort=${wsPort} camPort=${camPort} camWsPort=${camWsPort} camBaseUrl="${camBaseUrlEnv}"`);
});