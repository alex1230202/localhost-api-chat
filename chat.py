#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地聊天网页（自带服务，不依赖单独的代理进程）

用法
    python chat.py                 # 打开 http://127.0.0.1:8788
    python chat.py --port 9000

首次使用
    打开页面 → 中间「模型配置」栏 → 展开「服务商」 → 填「地址」和「Key」
    （例如 https://api.example.com/v1）→ 点「拉取模型列表」选一个模型 → 保存。

隐私
    本仓库不含任何 API Key、任何上游服务商地址，只有一个手填入口。
    填的地址和 Key 存在浏览器的 localStorage 里（不会落进仓库），只在你自己的机器
    与本地服务之间流转；服务端日志只记模型名 / 温度 / 轮数，不记地址与 Key。
要求
    Python 3.8+，无第三方依赖。Anthropic 协议的站才需要额外装 anthropic。

功能
    Markdown 流式输出 · 对话树（编辑 / 重新生成 / 复制 / 删除 / 切分支）· 本地存档
    · 角色预设 · 温度 / Top P / 两项 Penalty / Max Tokens / Seed 调参
    · 多模态图片地址 · 导出 Markdown / JSON / 文本
"""


import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 请求通用配置：UA / 超时 / 重试策略（原先放在中转代理模块里，该模块已移除，常量内联于此）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 300
MAX_RETRY = 6
# 触发重试的状态码：403=Cloudflare 拦 IP（出口轮换，单次约 30% 概率中招），其余为上游抖动
RETRY_CODES = {403, 429, 500, 502, 503, 504}

# 服务商检测结果缓存：探测要走真实小调用，7 个服务商全探一遍约 20 秒，
# 没必要每次开页面都重来。force=1 可强制刷新。
# 缓存同时让「服务商可用性」面板不用每次打开页面都等一轮探测。
PROV_CACHE_TTL = 900
_PROV_CACHE = {"ts": 0.0, "results": None}
PERSONAS = [
    "",
    "你是一个中文助手，回答简洁、直接、有条理。",
    "你是一个资深程序员，擅长用通俗的话解释技术，回答时给出可运行的代码示例。",
    "你是一个翻译官，擅长中英互译，翻译准确、自然，保留原文语气。",
    "你是一个写作编辑，擅长润色文字，让表达更流畅、更有感染力。",
    "你是一个小学老师，用大白话和生活中的例子解释任何问题。",
    "你是一个产品经理，习惯先问需求背景，再结构化地分析问题和给方案。",
    "你是一个严格的审稿人，会直接指出问题、逻辑漏洞和更好的做法，不客套。",
]

# 可切换的 API 服务商。protocol: anthropic = 走 /v1/messages；openai = 走 /v1/chat/completions
# 只保留已填 Key / 免 Key / 手填入口；未配置的官方站一律移除。
# 可切换的 API 服务商。protocol: anthropic = 走 /v1/messages；openai = 走 /v1/chat/completions
#
# 本仓库刻意只保留这一个「手填入口」：地址和 Key 由使用者自己在网页中间
# 「模型配置」栏填写，服务端不内置任何凭据，页面源码里也查不到。
#
# 想默认指向某个站（仅供自己 fork 时用），照填即可：
#     {"id": "custom", "name": "自定义（OpenAI 兼容）", "base": "https://api.example.com/v1",
#      "protocol": "openai", "key": "", "models": ["gpt-4o"]},
#       —— key 留空 = 让使用者在页面里填；不要把密钥提交进仓库。
# 接 Anthropic 协议的站：protocol 改成 "anthropic"，base 填不带 /v1 的根地址。
PROVIDERS = [
    {"id": "custom", "name": "自定义（OpenAI 兼容）", "base": "", "protocol": "openai", "models": []},
]

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地聊天</title>
<style>
/* 色值取自参考截图的实际像素（整图直方图）：
   底 #16161a 占比 95%、次级面 #1a1a1e、控件 #28282c / #323236、
   主蓝 #54a9ff（滑块·开关·主按钮·用户气泡）、文字 #f9f9f9、开关绿 #22e02a。
   左右两栏底色相同，靠 1px 边框分隔，不做深浅分区。 */
:root{
  --bg:#16161a; --panel:#16161a; --panel2:#1a1a1e; --ctl:#28282c; --ctl2:#323236;
  --line:#2a2a2e; --fg:#f2f2f5; --fg2:#a8a8b0; --fg3:#77777f;
  --blue:#54a9ff; --blue2:#3d8fe6; --cyan:#54a9ff; --green:#22e02a; --red:#ef4444;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--fg);
     height:100vh;display:flex;flex-direction:row;overflow:hidden}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#33333a;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* ---------- 左：对话历史 ---------- */
#sidebar{flex:0 0 210px;width:210px;background:var(--panel);border-right:1px solid var(--line);
         display:flex;flex-direction:column;height:100vh}
body.sb-collapsed #sidebar{display:none}
.shead{padding:12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--line)}
.shead .ttl{font-size:12px;font-weight:600;flex:1;color:var(--fg2);letter-spacing:.06em}
#slist{flex:1;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px}
.hrow{border:1px solid transparent;border-radius:8px;padding:8px 10px;display:flex;
      justify-content:space-between;align-items:center;gap:8px;background:var(--panel2)}
.hrow:hover{border-color:var(--line)}
.hrow.cur{border-color:var(--blue);background:#1b2130}
.htitle{font-size:12px;color:var(--fg);word-break:break-all}
.hmeta{font-size:11px;color:var(--fg3);margin-top:3px}
.hops{display:flex;gap:4px}

/* ---------- 中：模型配置 ---------- */
#cfgbar{flex:0 0 268px;width:268px;background:var(--panel);border-right:1px solid var(--line);
        display:flex;flex-direction:column;height:100vh}
body.cfg-collapsed #cfgbar{display:none}
#cfgscroll{flex:1;overflow-y:auto;padding:14px 14px 10px}
.cfgtop{padding:6px 2px 12px}
.cfgtop h1{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px;color:var(--fg)}
.fgroup{margin-bottom:14px}
.flab{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--fg2);
      letter-spacing:.04em;margin-bottom:6px}
.flab .val{margin-left:auto;font-size:11px;color:var(--fg);font-variant-numeric:tabular-nums}
.fdesc{font-size:10.5px;color:var(--fg3);margin-top:5px;line-height:1.5}
#cfgbar select,#cfgbar input[type=text],#cfgbar input[type=number],
#cfgbar input[type=password],#cfgbar textarea{
  width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;font-size:12px;
  background:var(--ctl);color:var(--fg);font-family:inherit;outline:none}
#cfgbar select:focus,#cfgbar input:focus,#cfgbar textarea:focus{border-color:var(--blue)}
#cfgbar select{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--fg2) 50%),
  linear-gradient(135deg,var(--fg2) 50%,transparent 50%);
  background-position:calc(100% - 14px) 50%,calc(100% - 9px) 50%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:26px}
/* 滑块 */
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:16px;background:transparent;cursor:pointer}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:var(--ctl2)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;
  background:var(--blue);margin-top:-4px;border:2px solid var(--bg)}
input[type=range]:disabled::-webkit-slider-thumb{background:var(--fg3)}
/* 开关 */
.sw{position:relative;width:34px;height:18px;flex:0 0 auto;margin-left:auto}
.sw input{position:absolute;opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:var(--ctl2);border-radius:9px;transition:.15s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:14px;height:14px;left:2px;top:2px;border-radius:50%;
  background:#cfcfd6;transition:.15s}
.sw input:checked + i{background:var(--blue)}
.sw input:checked + i:before{transform:translateX(16px);background:#fff}
.sw.on-green input:checked + i{background:var(--green)}
/* 折叠 */
.adv{border:1px solid var(--line);border-radius:8px;padding:9px 10px;background:var(--panel2);margin-bottom:14px}
.adv summary{font-size:11px;color:var(--fg2);cursor:pointer;user-select:none;list-style:none}
.adv summary::-webkit-details-marker{display:none}
.adv summary:before{content:"▸ ";color:var(--fg3)}
.adv[open] summary:before{content:"▾ "}
.adv .arow{margin-top:10px}
.adv label{display:block;font-size:11px;color:var(--fg2);margin-bottom:5px}
.pstatus-wrap{border-top:1px solid var(--line);margin-top:10px;padding-top:10px}
.pstatus-head{display:flex;justify-content:space-between;align-items:center;font-size:11px;
  color:var(--fg2);margin-bottom:8px}
#provstatus{display:flex;flex-direction:column;gap:5px}
.prow{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--fg2)}
.prow .pdot{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.prow .pdot.ok{background:var(--green)}
.prow .pdot.error{background:var(--red)}
.prow .pdot.unconfigured{background:#5a5a63}
.prow .pname{flex:0 0 108px;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prow .phint{color:var(--fg3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 底部固定操作条 */
.cfgfoot{border-top:1px solid var(--line);padding:10px 14px;display:flex;flex-direction:column;gap:8px}
.cfgsave-row{display:flex;gap:8px}
.cfgsave-row .btn{flex:1}
.cfgtime{font-size:10.5px;color:var(--fg3);display:flex;align-items:center;gap:6px}

/* ---------- 按钮 ---------- */
.btn{padding:6px 12px;border:1px solid var(--line);border-radius:7px;background:var(--ctl);
     color:var(--fg);font-size:12px;cursor:pointer;font-family:inherit;transition:.12s}
.btn:hover{background:var(--ctl2);border-color:#3a3a42}
.btn.pri{background:var(--blue);border-color:var(--blue);color:#fff}
.btn.pri:hover{background:var(--blue2);border-color:var(--blue2)}
.mini{font-size:11px;color:var(--fg2);background:transparent;border:1px solid var(--line);
      border-radius:6px;padding:2px 7px;cursor:pointer;font-family:inherit}
.mini:hover{background:var(--ctl);color:var(--fg)}
.mini:disabled{color:#4d4d55;cursor:not-allowed}
.mini.ix{border:0;background:transparent;cursor:default;padding:0 2px;color:var(--fg3)}
.icon-btn{background:transparent;border:0;color:var(--fg3);cursor:pointer;font-size:13px;padding:3px 6px;border-radius:5px}
.icon-btn:hover{background:var(--ctl);color:var(--fg)}
.hint{font-size:11px;color:var(--fg3);padding:4px 0}

/* ---------- 右：对话区 ---------- */
#content{flex:1;display:flex;flex-direction:column;min-width:0;height:100vh}
header{background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px;
       display:flex;align-items:center;gap:10px;flex-wrap:wrap}
header .ttl{font-size:14px;font-weight:600}
#submodel{font-size:11px;color:var(--cyan);font-family:Consolas,Menlo,monospace}
header .sp{flex:1}
header select{padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-size:11px;
  background:var(--ctl);color:var(--fg);font-family:inherit;cursor:pointer}
#state{font-size:11px;color:var(--fg3);min-width:60px;text-align:right}
#box{flex:1;overflow-y:auto;padding:22px 20px;display:flex;flex-direction:column;gap:4px}
.msg{max-width:820px;width:100%;margin:0 auto;padding:11px 15px;border-radius:12px;font-size:13.5px;
     line-height:1.75;white-space:pre-wrap;word-break:break-word}
.user{background:var(--blue);color:#fff;margin-right:0;margin-left:auto;width:fit-content;max-width:72%}
.ai{background:var(--panel2);border:1px solid var(--line);color:var(--fg)}
.err{background:#2a1618;border:1px solid #5c2226;color:#f08a8a}
.ctl{max-width:820px;width:100%;margin:0 auto 10px;display:flex;gap:6px;align-items:center;opacity:.55}
.ctl:hover{opacity:1}
.ctl.r{justify-content:flex-end}
.tag{display:inline-block;font-size:10.5px;color:#7ee2c8;background:#12312a;border:1px solid #1d5548;
     border-radius:4px;padding:1px 6px;margin-bottom:6px}
footer{border-top:1px solid var(--line);background:var(--bg);padding:12px 20px;}
.frow{display:flex;gap:10px;align-items:flex-end;max-width:820px;margin:0 auto}
#in{flex:1;resize:none;height:46px;max-height:200px;padding:13px 15px;border:1px solid var(--line);
    border-radius:22px;font-size:13.5px;background:var(--panel2);color:var(--fg);font-family:inherit;outline:none}
#in:focus{border-color:var(--blue)}
#in::placeholder{color:var(--fg3)}
#send{padding:0 18px;height:46px;border:0;border-radius:50%;background:var(--blue);color:#fff;
      font-size:15px;cursor:pointer;font-family:inherit;flex:0 0 auto;min-width:46px}
#send:disabled{background:#3a3a42;cursor:not-allowed}
#send.stop{background:var(--red);border-radius:22px;padding:0 18px;font-size:13px}
#files{display:flex;gap:8px;flex-wrap:wrap;max-width:820px;margin:0 auto 8px}
.chip{font-size:11px;background:#12312a;border:1px solid #1d5548;color:#7ee2c8;border-radius:6px;
      padding:3px 8px;display:flex;gap:6px;align-items:center}
.chip b{font-weight:400;cursor:pointer;color:var(--fg2)}
.dots{display:inline-flex;gap:4px;align-items:center;vertical-align:middle}
.dots i{width:6px;height:6px;border-radius:50%;background:var(--fg3);animation:b 1.2s infinite}
.dots i:nth-child(2){animation-delay:.2s}
.dots i:nth-child(3){animation-delay:.4s}
@keyframes b{0%,60%,100%{opacity:.25;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}
.footnote{font-size:10.5px;color:var(--fg3);text-align:center;padding-top:8px}
</style>
</head>
<body>
<!-- 一栏：对话历史 -->
<div id="sidebar">
  <div class="shead">
    <span class="ttl">对话历史</span>
    <button class="btn" id="sbnew">新建</button>
  </div>
  <div id="slist"></div>
</div>

<!-- 二栏：模型配置 -->
<div id="cfgbar">
  <div id="cfgscroll">
    <div class="cfgtop">
      <h1>&#9881; 模型配置</h1>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#9707; 模型</span></div>
      <input type="text" id="model" list="mlist" placeholder="模型名">
      <datalist id="mlist"></datalist>
    </div>

    <details class="adv" id="advsvc" open>
      <summary>服务商</summary>
      <div class="arow"><select id="prov"></select></div>
      <div class="arow"><label>地址</label><input type="text" id="abase" placeholder="https://api.example.com/v1"></div>
      <div class="arow"><label>Key</label><input type="password" id="akey" placeholder="留空 = 内置 Key"></div>
      <div class="arow"><button class="btn" id="fetchm" style="width:100%">拉取模型列表</button></div>
      <div class="fdesc" id="pinfo"></div>
      <div class="pstatus-wrap">
        <div class="pstatus-head">
          <span>可用性</span>
          <button class="btn" id="checkprov">重新检测</button>
        </div>
        <div id="provstatus"><span class="hint">检测中…</span></div>
      </div>
    </details>

    <details class="adv" id="apibox">
      <summary>角色设定</summary>
      <div class="arow"><select id="preset"></select></div>
      <div class="arow"><textarea id="persona" rows="3" placeholder="角色设定（可选），例如：你是一个善于讲故事的语文老师"></textarea></div>
    </details>

    <div class="fgroup">
      <div class="flab"><span>&#9673; 图片地址</span>
        <label class="sw" title="启用后可添加图片 URL 进行多模态对话"><input type="checkbox" id="imgon"><i></i></label>
      </div>
      <input type="text" id="imgurl" placeholder="https://example.com/image.jpg" disabled>
      <div class="fdesc">启用后可添加图片URL进行多模态对话</div>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#9878; Temperature</span><span class="val" id="tval">0.70</span></div>
      <input type="range" id="temp" min="0" max="100" step="5" value="70">
      <div class="fdesc">控制输出的随机性和创造性</div>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#9635; Top P</span><span class="val" id="v_top_p">1.00</span></div>
      <input type="range" id="a_top_p" min="0" max="1" step="0.05" value="1">
      <div class="fdesc">核采样，控制词汇选择的多样性</div>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#8646; Frequency Penalty</span><span class="val" id="v_fp">0.00</span></div>
      <input type="range" id="a_fp" min="-2" max="2" step="0.1" value="0">
      <div class="fdesc">频率惩罚，减少重复词汇的出现</div>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#8645; Presence Penalty</span><span class="val" id="v_pp">0.00</span></div>
      <input type="range" id="a_pp" min="-2" max="2" step="0.1" value="0">
      <div class="fdesc">存在惩罚，鼓励讨论新话题</div>
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#35; Max Tokens</span></div>
      <input type="number" id="a_max" min="64" step="128" placeholder="4096" value="4096">
    </div>

    <div class="fgroup">
      <div class="flab"><span>&#10021; Seed（可选，用于复现结果）</span></div>
      <input type="number" id="a_seed" step="1" placeholder="随机种子（留空为随机）">
    </div>

    <details class="adv">
      <summary>其它参数（上游可能不生效）</summary>
      <div class="arow"><label>top_k</label><input type="number" id="a_top_k" min="0" step="1" placeholder="留空 = 不发送"></div>
      <div class="arow"><label>停止词（逗号分隔，最多 4 个）</label><input type="text" id="a_stop" placeholder="留空 = 不发送"></div>
    </details>

    <div class="fgroup">
      <div class="flab"><span>&#9193; 流式输出</span>
        <label class="sw on-green"><input type="checkbox" id="streamon" checked><i></i></label>
      </div>
      <div class="fdesc">关闭后等整段生成完再显示</div>
    </div>
  </div>

  <div class="cfgfoot">
    <div class="cfgtime">
      <span id="cfgtime">上次保存：—</span>
      <button class="icon-btn" id="cfgrefresh" title="从本地存档重新读取">&#8635;</button>
    </div>
    <div class="cfgsave-row">
      <button class="btn pri" id="cfgsave">&#9881; 保存</button>
      <button class="btn" id="cfgload">&#8615; 导入</button>
    </div>
  </div>
</div>

<!-- 三栏：对话区 -->
<div id="content">
<header>
  <button class="btn" id="togglesb" title="显示/隐藏对话历史">&#9776;</button>
  <button class="btn" id="togglecfg" title="显示/隐藏模型配置">&#9881;</button>
  <div>
    <div class="ttl">AI 对话</div>
    <div id="submodel">—</div>
  </div>
  <div class="sp"></div>
  <select id="fmt"><option value="md">Markdown</option><option value="json">JSON</option><option value="txt">纯文本</option></select>
  <button class="btn" id="export">导出对话</button>
  <button class="btn" id="newchat">新建对话</button>
  <button class="btn" id="clear">清空对话</button>
  <span id="state">就绪</span>
</header>

<div id="box"></div>

<footer>
  <div id="files"></div>
  <div class="frow">
    <label class="icon-btn" for="file" title="添加附件" style="height:46px;line-height:40px;font-size:15px">&#128206;</label>
    <input type="file" id="file" multiple style="display:none">
    <textarea id="in" placeholder="请输入您的问题...（回车发送，Shift+回车换行）"></textarea>
    <button id="send">&#8593;</button>
  </div>
  <div class="footnote">对话自动保存到本机 chat_history 文件夹 · AI 回答下可「重新生成」「复制」「删除」或切版本</div>
</footer>
</div>

<script>
const MODELS = __MODELS__;
const PERSONAS = __PERSONAS__;
const box = document.getElementById('box');
const input = document.getElementById('in');
const sendBtn = document.getElementById('send');
const sel = document.getElementById('model');
const state = document.getElementById('state');
const tempEl = document.getElementById('temp');
const tval = document.getElementById('tval');
const preset = document.getElementById('preset');
const persona = document.getElementById('persona');
const fileInput = document.getElementById('file');
const filesBox = document.getElementById('files');

/* ---------- API 服务商 ---------- */
const PROV = __PROVIDERS__;
const prov = document.getElementById('prov');
const abase = document.getElementById('abase');
const akey = document.getElementById('akey');
const pinfo = document.getElementById('pinfo');
const fetchm = document.getElementById('fetchm');
const mlist = document.getElementById('mlist');
const sidebar = document.getElementById('sidebar');
const slist = document.getElementById('slist');
const sbnew = document.getElementById('sbnew');
const togglesb = document.getElementById('togglesb');

PROV.forEach(p => { const o=document.createElement('option'); o.value=p.id; o.textContent=p.name; prov.appendChild(o); });
function provOf(id){ return PROV.find(p => p.id === id) || PROV[0]; }
function setModelOptions(list){
  mlist.innerHTML='';
  (list||[]).forEach(m => { const o=document.createElement('option'); o.value=m; mlist.appendChild(o); });
}
/* ---------- 模型配置：保存 / 导入 ---------- */
function cfgSnapshot(){
  const g = id => document.getElementById(id).value;
  return {prov:prov.value, base:abase.value, key:akey.value, model:sel.value,
          temp:g('temp'),
          top_p:g('a_top_p'), fp:g('a_fp'), pp:g('a_pp'),
          max:g('a_max'), seed:g('a_seed'), top_k:g('a_top_k'), stop:g('a_stop'),
          persona:persona.value, preset:preset.value,
          imgon:document.getElementById('imgon').checked, imgurl:g('imgurl'),
          stream:document.getElementById('streamon').checked,
          saved_at:new Date().toISOString()};
}
function saveCfg(){
  try{
    const cfg = cfgSnapshot();
    localStorage.setItem('apicfg', JSON.stringify(cfg));
    markSaved(cfg.saved_at);
  }catch(e){}
}
function loadCfg(){ try{ return JSON.parse(localStorage.getItem('apicfg')||'{}'); }catch(e){ return {}; } }
function markSaved(iso){
  const el = document.getElementById('cfgtime');
  if(!el) return;
  let txt = iso || '';
  try{ txt = new Date(iso || Date.now()).toLocaleString(); }catch(e){ txt = String(iso||''); }
  el.textContent = '上次保存：' + txt;
}
function syncRangeLabels(){
  const g = id => document.getElementById(id).value;
  document.getElementById('tval').textContent = (parseFloat(g('temp'))/100).toFixed(2);
  document.getElementById('v_top_p').textContent = parseFloat(g('a_top_p')).toFixed(2);
  document.getElementById('v_fp').textContent = parseFloat(g('a_fp')).toFixed(2);
  document.getElementById('v_pp').textContent = parseFloat(g('a_pp')).toFixed(2);
}
function syncImgState(){
  const on = document.getElementById('imgon').checked;
  const u = document.getElementById('imgurl');
  u.disabled = !on;
  if(!on) u.value = '';
}
function applyCfg(cfg){
  cfg = cfg || {};
  if(cfg.prov && provOf(cfg.prov).id === cfg.prov) prov.value = cfg.prov;
  const p = provOf(prov.value);
  abase.value = (cfg.base !== undefined && cfg.base !== '') ? cfg.base : p.base;
  akey.value = (cfg.key !== undefined && cfg.key !== '') ? cfg.key : '';
  akey.placeholder = p.hasKey ? '已内置 Key，留空即可' : '需要手填 Key';
  setModelOptions(provModels(p));
  if(cfg.model) sel.value = cfg.model;
  else if(provModels(p).length) sel.value = provModels(p)[0];
  const put = (id, v) => {
    if(v !== undefined && v !== null && v !== '') document.getElementById(id).value = v;
  };
  put('temp', cfg.temp);
  put('a_top_p', cfg.top_p); put('a_fp', cfg.fp); put('a_pp', cfg.pp);
  put('a_max', cfg.max); put('a_seed', cfg.seed);
  put('a_top_k', cfg.top_k); put('a_stop', cfg.stop);
  if(cfg.persona) persona.value = cfg.persona;
  if(cfg.preset) preset.value = cfg.preset;
  if(cfg.imgurl) document.getElementById('imgurl').value = cfg.imgurl;
  document.getElementById('imgon').checked = !!cfg.imgon;
  document.getElementById('streamon').checked = (cfg.stream === undefined) ? true : !!cfg.stream;
  syncRangeLabels(); syncImgState(); updateSub(); provUI();
}
function provModels(p){ return (p.models && p.models.length) ? p.models : (p.protocol === 'anthropic' ? MODELS : []); }
function provUI(){
  const p = provOf(prov.value);
  pinfo.textContent = (p.protocol === 'anthropic' ? 'Anthropic 协议' : 'OpenAI 兼容协议') +
    (p.models && p.models.length ? ' · 内置模型 ' + p.models.length + ' 个' : ' · 需手填模型名');
}
prov.onchange = () => {
  const p = provOf(prov.value);
  // key 不下发到网页，切换服务商时清空；留空即使用服务端内置 key
  abase.value = p.base; akey.value = '';
  akey.placeholder = p.hasKey ? '已内置 Key，留空即可' : '需要手填 Key';
  setModelOptions(provModels(p));
  if(provModels(p).length) sel.value = provModels(p)[0];
  provUI(); updateSub(); saveCfg();
};
abase.oninput = saveCfg;
akey.oninput = saveCfg;
sel.addEventListener('input', () => { saveCfg(); updateSub(); });
// datalist 会按当前值过滤（框里有字就只显示匹配项），点进来时清空好让全部选项都露出来
let modelBefore = '';
sel.addEventListener('focus', () => { modelBefore = sel.value; sel.value = ''; });
sel.addEventListener('blur', () => { if(!sel.value.trim()) sel.value = modelBefore; });
fetchm.onclick = async () => {
  let base = abase.value.trim();
  while(base.endsWith('/')) base = base.slice(0, -1);
  if(!base){
    if(provOf(prov.value).protocol === 'anthropic'){ setModelOptions(MODELS); if(!sel.value) sel.value = MODELS[0]; setState('已恢复默认模型列表'); }
    else alert('先填 API 地址，比如 https://api.example.com/v1');
    return;
  }
  try{
    setState('拉取模型列表中');
    const r = await fetch('/list_models', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base: base, key: akey.value.trim(), protocol: provOf(prov.value).protocol, prov: prov.value})});
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    if(!d.models || !d.models.length) throw new Error('返回的模型列表是空的');
    setModelOptions(d.models);
    if(!d.models.includes(sel.value)) sel.value = d.models[0];
    setState('拉到 ' + d.models.length + ' 个模型');
    updateSub(); saveCfg();
  }catch(e){ alert('拉取失败：' + e.message); setState('就绪'); }
};
function apiCfg(){
  let base = abase.value.trim();
  while(base.endsWith('/')) base = base.slice(0, -1);
  return {base: base, key: akey.value.trim(), protocol: provOf(prov.value).protocol, prov: prov.value};
}
(function initCfg(){
  const cfg = loadCfg();
  applyCfg(cfg);
  markSaved(cfg.saved_at || new Date().toISOString());
})();

/* 滑块 / 开关 / 保存导入 的交互绑定 */
['temp','a_top_p','a_fp','a_pp'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', () => { syncRangeLabels(); saveCfg(); });
});
document.getElementById('imgon').onchange = () => { syncImgState(); saveCfg(); };
document.getElementById('imgurl').oninput = () => saveCfg();
document.getElementById('streamon').onchange = () => saveCfg();
document.getElementById('cfgsave').onclick = () => { saveCfg(); setState('配置已保存'); };
document.getElementById('cfgload').onclick = () => { applyCfg(loadCfg()); setState('已导入本地配置'); };
document.getElementById('cfgrefresh').onclick = () => {
  applyCfg(loadCfg()); markSaved(loadCfg().saved_at); renderSidebar(); setState('已重新读取配置');
};

/* ---------- 服务商可用性检测 ---------- */
const checkprov = document.getElementById('checkprov');
const provstatus = document.getElementById('provstatus');
const PROV_CACHE_KEY = 'provstatus_cache';
let provMap = {};   // id -> 最近一次检测结果，供「默认落到可用项」与故障转移参考
function _statusClass(s){ return s === 'ok' ? 'ok' : s === 'error' ? 'error' : 'unconfigured'; }
function _ageText(sec){
  if(sec < 60) return sec + ' 秒前';
  if(sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  return Math.floor(sec / 3600) + ' 小时前';
}
function _renderProv(d, note){
  const map = {};
  (d.results || []).forEach(x => { map[x.id] = x; });
  provMap = map;
  provstatus.innerHTML = '';
  PROV.forEach(p => {
    const x = map[p.id] || {status:'unconfigured', detail:''};
    const row = document.createElement('div'); row.className = 'prow';
    row.innerHTML = `<span class="pdot ${_statusClass(x.status)}"></span>` +
                    `<span class="pname">${p.name}</span>` +
                    `<span class="phint">${x.detail || ''}</span>`;
    provstatus.appendChild(row);
  });
  if(note){
    const t = document.createElement('div'); t.className = 'hint'; t.textContent = note;
    provstatus.appendChild(t);
  }
}
async function checkProviders(force){
  if(!checkprov) return;
  checkprov.disabled = true;
  // 先渲染本地缓存，避免每次开页面都空着等 20 秒
  if(!force){
    try{
      const c = JSON.parse(localStorage.getItem(PROV_CACHE_KEY) || 'null');
      if(c && c.results && c.results.length){
        _renderProv(c, '上次检测（' + _ageText(Math.floor(Date.now()/1000) - c.ts) + '）');
      }
    }catch(e){}
  }else{
    provstatus.innerHTML = '<span class="hint">重新检测中…</span>';
  }
  try{
    const cur = loadCfg();
    const overrides = {};
    if(cur && cur.prov){ overrides[cur.prov] = {base: cur.base || '', key: cur.key || ''}; }
    const r = await fetch('/check_providers', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({overrides: overrides, force: !!force})});
    const d = await r.json();
    if(!d.results || !d.results.length) throw new Error('返回为空');
    _renderProv(d, d.cached ? ('服务端缓存（' + _ageText(d.age || 0) + '）· 点「重新检测」强制刷新') : '刚刚检测');
    try{
      localStorage.setItem(PROV_CACHE_KEY, JSON.stringify({ts: Math.floor(Date.now()/1000), results: d.results}));
    }catch(e){}
    autoPickProvider(d.results);
  }catch(e){
    provstatus.innerHTML = '<span class="hint">检测失败：' + (e && e.message ? e.message : e) + '</span>';
  }finally{
    checkprov.disabled = false;
  }
}
// 当前选中的服务商检测为不可用时，自动落到第一个可用项
function autoPickProvider(results){
  if(!results || !results.length) return;
  try{
    const cfg = loadCfg();
    const cur = cfg.prov || prov.value;
    const curRes = results.find(x => x.id === cur);
    if(curRes && curRes.status === 'ok') return;       // 当前可用就不动
    // 当前项未填过 Key 且用户手动改过地址时，也不强行切走
    const touched = cfg.base || cfg.key;
    if(curRes && curRes.status === 'unconfigured' && touched) return;
    const ok = results.find(x => x.status === 'ok');
    if(!ok) return;
    const p = provOf(ok.id);
    prov.value = ok.id;
    abase.value = p.base || '';
    akey.value = '';
    akey.placeholder = p.hasKey ? '已内置 Key，留空即可' : '需要手填 Key';
    setModelOptions(provModels(p));
    if(provModels(p).length) sel.value = provModels(p)[0];
    provUI(); updateSub(); saveCfg();
    setState('已自动切换到可用的「' + p.name + '」');
  }catch(e){}
}
checkprov && (checkprov.onclick = () => checkProviders(true));
// 打开页面自动检测一次（非强制，命中缓存可秒出）
checkProviders(false);

const PRESET_NAMES = ['（不设定）','通用助手','资深程序员','翻译官','写作编辑','小学老师','产品经理','严格审稿人'];
PERSONAS.forEach((p,i) => { const o=document.createElement('option'); o.value=p; o.textContent=PRESET_NAMES[i]||('预设'+i); preset.appendChild(o); });
preset.onchange = () => { persona.value = preset.value; };

/* ---------- 对话树 ---------- */
let nodes = {};        // id -> {id, role, content, parent, children:[]}
let currentId = null;  // 当前活跃路径的末端节点
let seq = 0;
let busy = false;
let timer = null;
let pendingFiles = [];

function addNode(role, content, parent){
  const id = 'n' + (++seq);
  nodes[id] = {id, role, content: content, parent: parent, children: []};
  if(parent && nodes[parent]) nodes[parent].children.push(id);
  return id;
}
function pathTo(id){
  const out = []; let n = id;
  while(n){ out.unshift(n); n = nodes[n].parent; }
  return out;
}
function sibsOf(id){
  const p = nodes[id].parent;
  return p ? nodes[p].children : [id];
}
function historyTo(id){
  return pathTo(id).map(x => ({role: nodes[x].role, content: nodes[x].content}));
}

function scroll(){ box.scrollTop = box.scrollHeight; }
function setState(t){
  state.textContent = t;
  if(timer){ clearInterval(timer); timer = null; }
  if(t.indexOf('中') >= 0){
    const s = Date.now();
    timer = setInterval(()=>{ state.textContent = t + ' ' + ((Date.now()-s)/1000).toFixed(0) + 's'; }, 500);
  }
}
tempEl.oninput = () => { tval.textContent = (tempEl.value/100).toFixed(2); };

/* ---------- 渲染 ---------- */
function render(){
  box.innerHTML = '';
  if(!currentId) return;
  pathTo(currentId).forEach(nid => {
    const n = nodes[nid];
    const el = document.createElement('div');
    el.className = 'msg ' + (n.role === 'user' ? 'user' : 'ai');
    el.textContent = n.content;
    box.appendChild(el);

    const bar = document.createElement('div');
    bar.className = 'ctl' + (n.role === 'user' ? ' r' : '');
    const sibs = sibsOf(nid);
    const idx = sibs.indexOf(nid);

    if(sibs.length > 1){
      const prev = document.createElement('button'); prev.className='mini'; prev.textContent='‹';
      prev.disabled = idx === 0;
      prev.onclick = () => { currentId = sibs[idx-1]; render(); scheduleSave(); };
      const ix = document.createElement('span'); ix.className='mini ix';
      ix.textContent = (idx+1) + '/' + sibs.length;
      const next = document.createElement('button'); next.className='mini'; next.textContent='›';
      next.disabled = idx === sibs.length-1;
      next.onclick = () => { currentId = sibs[idx+1]; render(); scheduleSave(); };
      bar.appendChild(prev); bar.appendChild(ix); bar.appendChild(next);
    }
    if(n.role === 'assistant'){
      const r = document.createElement('button'); r.className='mini'; r.textContent='↻ 重新生成';
      r.onclick = () => { currentId = nodes[nid].parent; render(); generate(nodes[nid].parent); };
      bar.appendChild(r);
    } else {
      const e = document.createElement('button'); e.className='mini'; e.textContent='✎ 编辑';
      e.onclick = () => editUser(nid);
      bar.appendChild(e);
    }
    const cp = document.createElement('button'); cp.className='mini'; cp.textContent='⧉ 复制';
    cp.onclick = () => copyMsg(nid);
    bar.appendChild(cp);
    const delBtn = document.createElement('button'); delBtn.className='mini'; delBtn.textContent='🗑 删除';
    delBtn.onclick = () => delMsg(nid);
    bar.appendChild(delBtn);
    box.appendChild(bar);
  });
  scroll();
}

/* 复制 / 删除单条消息 */
function copyText(t){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(() => setState('已复制'),
      () => { fallbackCopy(t); setState('已复制'); });
  } else { fallbackCopy(t); setState('已复制'); }
}
function fallbackCopy(t){
  const ta = document.createElement('textarea');
  ta.value = t; ta.style.position='fixed'; ta.style.top='-1000px';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); }catch(e){}
  document.body.removeChild(ta);
}
function copyMsg(nid){
  const t = nodes[nid] && nodes[nid].content;
  if(!t){ setState('这条没有内容可复制'); return; }
  copyText(t);
}
function delMsg(nid){
  if(busy) return;
  const node = nodes[nid];
  if(!node) return;
  if(!confirm('删除这条' + (node.role === 'user' ? '提问' : '回答') + '？其下的分支会一并移除。')) return;
  const doomed = [];
  (function walk(x){ doomed.push(x); (nodes[x].children || []).forEach(walk); })(nid);
  const p = node.parent;
  if(p && nodes[p]) nodes[p].children = nodes[p].children.filter(x => x !== nid);
  doomed.forEach(x => { delete nodes[x]; });
  if(doomed.indexOf(currentId) >= 0) currentId = p || null;
  render(); scheduleSave(); setState('已删除');
}

function editUser(nid){
  if(busy) return;
  const v = prompt('编辑这条消息（会生成一条新分支，原消息保留）：', nodes[nid].content);
  if(v === null || !v.trim()) return;
  const uid = addNode('user', v.trim(), nodes[nid].parent);
  currentId = uid;
  render();
  generate(uid);
}

/* ---------- 生成 ---------- */
function advParams(){
  const g = id => document.getElementById(id).value.trim();
  const p = {};
  // 滑块类参数始终有值（模型配置栏里的 Temperature / Top P / 两个 Penalty），直接发送
  p.top_p = parseFloat(g('a_top_p'));
  p.frequency_penalty = parseFloat(g('a_fp'));
  p.presence_penalty = parseFloat(g('a_pp'));
  if(g('a_top_k')!=='') p.top_k = parseInt(g('a_top_k'));
  if(g('a_max')!=='')   p.max_tokens = parseInt(g('a_max'));
  if(g('a_seed')!=='')  p.seed = parseInt(g('a_seed'));
  if(g('a_stop')!=='')  p.stop = g('a_stop').split(/[，,]/).map(s=>s.trim()).filter(Boolean).slice(0,4);
  return p;
}
// 图片地址：开启后随请求下发，由服务端拼成多模态 content 数组
function imgParam(){
  const on = document.getElementById('imgon').checked;
  const u = document.getElementById('imgurl').value.trim();
  return (on && u) ? u : '';
}
function streamOn(){ return document.getElementById('streamon').checked; }
function updateSub(){
  const p = provOf(prov.value);
  document.getElementById('submodel').textContent = '[' + p.id + ']' + (sel.value || '未选模型');
}

let controller = null;          // 生成中的 AbortController，用于「停止」
function stopGen(){
  if(controller){ controller.abort(); controller = null; }
}
async function generate(parentId){
  if(busy) return;
  busy = true;
  controller = new AbortController();
  sendBtn.disabled = false; sendBtn.textContent = '停止'; sendBtn.classList.add('stop');
  setState('生成中');

  const think = document.createElement('div');
  think.className = 'msg ai';
  think.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> 思考中…';
  box.appendChild(think); scroll();

  const aid = addNode('assistant', '', parentId);
  let el = null, full = '', tagged = false, aborted = false;

  try{
    const res = await fetch('/chat', {method:'POST', headers:{'Content-Type':'application/json'},
      signal: controller.signal,
      body: JSON.stringify({model: sel.value, messages: historyTo(parentId),
                            temperature: tempEl.value/100, persona: persona.value.trim(),
                            params: advParams(), api: apiCfg(), image: imgParam()})});
    if(!res.ok) throw new Error('服务返回 ' + res.status);
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while(true){
      const {done, value} = await reader.read(); if(done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\\n'); buf = lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: ')) continue;
        let evt; try{ evt = JSON.parse(line.slice(6)); }catch(e){ continue; }
        if(evt.type === 'status'){
          think.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> ' + evt.text;
          setState(evt.text);
        } else if(evt.type === 'delta'){
          if(!el){
            think.remove();
            el = document.createElement('div');
            el.className = 'msg ai';
            box.appendChild(el);
            const s = document.createElement('span'); el.appendChild(s); el.txtNode = s;
          }
          full += evt.text;
          if(streamOn()){ el.txtNode.textContent = full; scroll(); }
          else { el.txtNode.textContent = '（正在生成…已 ' + full.length + ' 字）'; }
        } else if(evt.type === 'error'){ throw new Error(evt.message); }
      }
    }
    nodes[aid].content = full;
    currentId = aid;
  }catch(e){
    aborted = (e && e.name === 'AbortError');
    if(aborted){
      // 用户主动停止：保留已生成的部分，不报错
      nodes[aid].content = full || '（已停止）';
      if(full) currentId = aid; else currentId = parentId || null;
      if(think.parentNode) think.remove();
      if(!el && full){ el = document.createElement('div'); el.className='msg ai'; box.appendChild(el); }
      setState('已停止');
    }else{
      // 失败：把这个空节点从树上摘掉
      const p = nodes[aid].parent;
      if(p){ nodes[p].children = nodes[p].children.filter(x => x !== aid); }
      delete nodes[aid];
      if(think.parentNode) think.remove();
      if(el) el.remove();
      const errEl = document.createElement('div'); errEl.className = 'msg err';
      errEl.textContent = '出错了：' + e.message + '（确认 chat.py 那个窗口还开着）';
      box.appendChild(errEl);
      currentId = parentId || null;
    }
  }
  controller = null;
  sendBtn.textContent = '发送'; sendBtn.classList.remove('stop');
  busy = false; sendBtn.disabled = false;
  if(!aborted) setState('就绪');
  render();
  scheduleSave();
  input.focus();
}

async function send(){
  if(busy) return;
  const text = input.value.trim();
  if(!text && !pendingFiles.length) return;

  let content = text;
  if(pendingFiles.length){
    content = (text ? text + '\\n\\n' : '') + pendingFiles.map(f =>
      '===== 附件：' + f.name + ' =====\\n' + f.text).join('\\n\\n');
  }
  const uid = addNode('user', content, currentId);
  currentId = uid;
  pendingFiles = []; renderFiles();
  input.value = '';
  render();
  generate(uid);
}

/* ---------- 附件 ---------- */
function renderFiles(){
  filesBox.innerHTML = '';
  pendingFiles.forEach((f,i) => {
    const c = document.createElement('div'); c.className = 'chip';
    c.textContent = f.name + ' (' + Math.round(f.text.length/1000) + 'k字) ';
    const x = document.createElement('b'); x.textContent = '×';
    x.onclick = () => { pendingFiles.splice(i,1); renderFiles(); };
    c.appendChild(x); filesBox.appendChild(c);
  });
}
function loadScript(src){
  return new Promise((res,rej)=>{ const s=document.createElement('script'); s.src=src; s.onload=res; s.onerror=rej; document.head.appendChild(s); });
}
async function readPdf(file){
  if(!window.pdfjsLib){
    const V='4.0.379';
    await loadScript('https://cdn.jsdelivr.net/npm/pdfjs-dist@'+V+'/build/pdf.min.js');
    window.pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@'+V+'/build/pdf.worker.min.js';
  }
  const doc = await window.pdfjsLib.getDocument({data: await file.arrayBuffer()}).promise;
  let out=[];
  for(let i=1;i<=Math.min(doc.numPages,40);i++){
    const p = await doc.getPage(i); const c = await p.getTextContent();
    out.push(c.items.map(s=>s.str).join(' '));
  }
  return out.join('\\n');
}
const TEXT_EXT = ['txt','md','markdown','csv','json','js','ts','py','java','go','c','cpp','h','html','css','xml','yml','yaml','log','sql','sh','bat','ini','conf'];
fileInput.onchange = async () => {
  for(const f of fileInput.files){
    const ext = (f.name.split('.').pop()||'').toLowerCase();
    try{
      let text='';
      if(ext==='pdf'){ text = await readPdf(f); }
      else if(TEXT_EXT.includes(ext) || f.size < 300*1024){ text = await f.text(); }
      else { alert('暂不支持这个文件类型：'+f.name); continue; }
      if(text.length > 60000) text = text.slice(0,60000) + '\\n...(已截断)';
      pendingFiles.push({name:f.name, text:text});
    }catch(e){ alert('读取失败：'+f.name+'\\n'+e.message); }
  }
  fileInput.value='';
  renderFiles();
};

/* ---------- 导出 ---------- */
function pad(n){ return String(n).padStart(2,'0'); }
function stamp(){
  const d=new Date();
  return ''+d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate())+'-'+pad(d.getHours())+pad(d.getMinutes());
}
function exportChat(){
  const hist = currentId ? historyTo(currentId) : [];
  if(!hist.length){ alert('还没有对话内容，先聊两句吧。'); return; }
  const fmt = document.getElementById('fmt').value;
  const meta = {exported_at:new Date().toISOString(), model:sel.value,
                temperature:tempEl.value/100, persona:persona.value.trim()||null};
  let content, ext;
  if(fmt==='json'){
    content = JSON.stringify(Object.assign({messages:hist}, meta), null, 2); ext='json';
  } else if(fmt==='txt'){
    content = ['对话记录','时间：'+new Date().toLocaleString(),'模型：'+sel.value,
               '温度：'+(tempEl.value/100),'角色设定：'+(persona.value.trim()||'（无）'),
               '='.repeat(30),''].join('\\n')
      + hist.map(m=>(m.role==='user'?'我：':'AI：')+m.content).join('\\n\\n') + '\\n';
    ext='txt';
  } else {
    content = ['# 对话记录','','- 时间：'+new Date().toLocaleString(),'- 模型：'+sel.value,
               '- 温度：'+(tempEl.value/100),'- 角色设定：'+(persona.value.trim()||'（无）'),
               '','---',''].join('\\n')
      + hist.map(m=>'### '+(m.role==='user'?'我':'AI')+'\\n\\n'+m.content+'\\n').join('\\n');
    ext='md';
  }
  const blob = new Blob([content],{type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = '对话记录-' + stamp() + '.' + ext;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  setState('已导出 ' + hist.length + ' 条');
}

/* ---------- 本地存档 ---------- */
let sessionId = null;
let saveTimer = null;

function titleOf(){
  if(!currentId) return '新对话';
  for(const id of pathTo(currentId)){
    if(nodes[id].role === 'user')
      return nodes[id].content.split('\\n')[0].slice(0,40);
  }
  return '新对话';
}
function scheduleSave(){
  if(!sessionId) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 700);
}
async function saveNow(){
  if(!sessionId) return;
  try{
    await fetch('/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: sessionId, title: titleOf(), nodes: nodes, currentId: currentId})});
    setState('已保存');
    renderSidebar();
  }catch(e){ /* 存不上也不影响聊天 */ }
}
window.addEventListener('beforeunload', () => {
  if(sessionId && currentId){
    navigator.sendBeacon('/save', new Blob([JSON.stringify(
      {id: sessionId, title: titleOf(), nodes: nodes, currentId: currentId})],
      {type:'application/json'}));
  }
});

function resetTree(){
  nodes = {}; currentId = null; seq = 0; pendingFiles = [];
  box.innerHTML = ''; renderFiles();
}
function newSession(){
  resetTree();
  sessionId = 's' + Date.now();
  setState('新对话');
  renderSidebar();
}
async function loadSession(id){
  const r = await fetch('/session?id=' + encodeURIComponent(id));
  const d = await r.json();
  if(!d || !d.nodes){ alert('读取失败'); return; }
  resetTree();
  sessionId = id;
  nodes = d.nodes || {};
  currentId = d.currentId || null;
  seq = Object.keys(nodes).reduce((m,k)=>Math.max(m, parseInt(k.slice(1))||0), 0);
  render();
  setState('已载入');
  renderSidebar();
}
async function renderSidebar(){
  try{
    const r = await fetch('/sessions');
    const d = await r.json();
    const list = d.sessions || [];
    slist.innerHTML = '';
    if(!list.length){
      const e = document.createElement('div'); e.className='hmeta'; e.style.padding='6px';
      e.textContent='还没有保存过对话。';
      slist.appendChild(e);
      return;
    }
    list.forEach(s => {
      const row = document.createElement('div');
      row.className = 'hrow' + (s.id === sessionId ? ' cur' : '');
      const left = document.createElement('div'); left.style.flex='1';
      const t = document.createElement('div'); t.className='htitle'; t.textContent = s.title;
      const m = document.createElement('div'); m.className='hmeta';
      let when = s.updated_at || '';
      if(when){ try{ when = new Date(when.replace(' ','T')).toLocaleString(); }catch(e){} }
      m.textContent = when + ' · ' + s.count + ' 条';
      left.appendChild(t); left.appendChild(m);
      const ops = document.createElement('div'); ops.className='hops';
      const b1 = document.createElement('button'); b1.className='mini'; b1.textContent='进入';
      b1.onclick = async () => { await loadSession(s.id); };
      const b2 = document.createElement('button'); b2.className='mini'; b2.textContent='删除';
      b2.onclick = async () => {
        if(!confirm('删除这条记录？「' + s.title + '」')) return;
        await fetch('/session?id=' + encodeURIComponent(s.id), {method:'DELETE'});
        if(s.id === sessionId) newSession();
        renderSidebar();
      };
      ops.appendChild(b1); ops.appendChild(b2);
      row.appendChild(left); row.appendChild(ops); slist.appendChild(row);
    });
  }catch(e){ /* 侧栏拉不到也不影响聊天 */ }
}

async function init(){
  try{
    const r = await fetch('/sessions');
    const d = await r.json();
    if(d.sessions && d.sessions.length) await loadSession(d.sessions[0].id);
    else newSession();
  }catch(e){ newSession(); }
}

document.getElementById('clear').onclick = () => {
  resetTree(); sessionId = 's' + Date.now();
};
document.getElementById('export').onclick = exportChat;
document.getElementById('newchat').onclick = () => { newSession(); input.focus(); };
sbnew.onclick = () => { newSession(); input.focus(); };
togglesb.onclick = () => { document.body.classList.toggle('sb-collapsed'); };
document.getElementById('togglecfg').onclick = () => { document.body.classList.toggle('cfg-collapsed'); };

sendBtn.onclick = () => { if(controller) stopGen(); else send(); };
input.addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } });

init();
input.focus();
</script>
</body>
</html>
"""

HIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history")


def _safe_id(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(sid))[:64]


def _hist_path(sid):
    return os.path.join(HIST_DIR, _safe_id(sid) + ".json")


def ensure_hist_dir():
    if not os.path.isdir(HIST_DIR):
        os.makedirs(HIST_DIR, exist_ok=True)


def list_sessions():
    ensure_hist_dir()
    out = []
    for name in os.listdir(HIST_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(HIST_DIR, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        out.append({
            "id": d.get("id", name[:-5]),
            "title": d.get("title", "（无标题）"),
            "updated_at": d.get("updated_at", ""),
            "count": len(d.get("nodes") or {}),
        })
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


def load_session(sid):
    p = _hist_path(sid)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(data):
    ensure_hist_dir()
    sid = _safe_id(data.get("id") or ("s" + str(int(time.time() * 1000))))
    record = {
        "id": sid,
        "title": (data.get("title") or "新对话")[:60],
        "nodes": data.get("nodes") or {},
        "currentId": data.get("currentId"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(_hist_path(sid), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    return sid


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _anthropic_sdk():
    """延迟导入官方 anthropic 同步 SDK（只有 protocol 为 anthropic 时才需要）。

    部分 Anthropic 兼容站的 WAF 会在连接层按 TLS 握手指纹放行：同一把 key，
    官方同步 Anthropic() 通过、AsyncAnthropic() 与 urllib 都被拒，
    差异只在同步 httpx 的握手方式，所以只能用官方同步 SDK。"""
    try:
        import anthropic  # type: ignore
        return anthropic
    except Exception:
        raise RuntimeError(
            "该服务商需要官方 anthropic 库，请先在本机执行：python -m pip install anthropic")


def open_api(base, key, path, payload=None, extra=None,
             timeout=TIMEOUT, max_retry=MAX_RETRY):
    """请求自定义 API（OpenAI 兼容 / Anthropic 皆可）。base 不带末尾斜杠，如
    https://api.example.com/v1。payload 为 None 时是 GET，否则 POST。

    timeout / max_retry 可调：故障转移链里后面还有候选时，单个站不该重试 6 次、
    每次等满 300 秒——那样切一次站要两分钟。快速失败模式把它们压到 30s / 1 次。
    """
    url = base.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "User-Agent": UA,
        "Accept-Encoding": "identity",
    }
    if extra:
        headers.update(extra)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    last = None
    for attempt in range(max(1, max_retry)):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in RETRY_CODES and attempt < max_retry - 1:
                wait = 1.0 + 0.8 * attempt
                log(f"  [{e.code}] {path} retry {attempt + 1}/{max_retry - 1} in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < max_retry - 1:
                wait = 1.0 + 0.8 * attempt
                log(f"  [net] {e} retry {attempt + 1}/{max_retry - 1} in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
    raise last


def public_providers():
    """下发给前端的服务商清单：剔除 key，只给 hasKey 标记（key 不进网页源码）。"""
    out = []
    for p in PROVIDERS:
        q = {k: v for k, v in p.items() if k != "key"}
        q["hasKey"] = bool(p.get("key")) or bool(p.get("nokey"))
        out.append(q)
    return out


def prov_key(prov_id, override=""):
    """取服务商 key：手填的优先，否则用内置 key。"""
    k = (override or "").strip()
    if k:
        return k
    for p in PROVIDERS:
        if p["id"] == prov_id:
            return (p.get("key") or "").strip()
    return ""


def _short(s, n=150):
    s = str(s).replace("\n", " ").strip()
    return s[:n]


def refresh_prov_cache_async():
    """后台静默刷新服务商检测结果，让可用性面板能立刻出结果。

    检测要跑真实小调用，同步等会把请求卡住十几秒，所以只在缓存过期时
    起守护线程跑；busy 标志保证同一时刻只有一个线程在刷。
    """
    if _PROV_CACHE.get("busy"):
        return
    if _PROV_CACHE.get("results") and time.time() - _PROV_CACHE["ts"] < PROV_CACHE_TTL:
        return
    _PROV_CACHE["busy"] = True

    def run():
        try:
            from concurrent.futures import ThreadPoolExecutor

            def worker(p):
                base = (p.get("base") or "").strip().rstrip("/")
                return _check_one(p, base, prov_key(p["id"]), p.get("protocol", "openai"))

            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(worker, PROVIDERS))
            _PROV_CACHE["ts"] = time.time()
            _PROV_CACHE["results"] = results
        except Exception:
            pass
        finally:
            _PROV_CACHE["busy"] = False

    threading.Thread(target=run, daemon=True).start()


def _check_one(p, base, key, protocol):
    """检测单个服务商可用性：返回 {id,name,ok,status,detail}。ok=None 表示未配置。

    服务商互不相干，单项失败必须兜底：任何未预期的异常都要变成这一项的红灯，
    绝不能抛出——否则线程池把它冒到 /check_providers 顶端，整个页面会全红。
    """
    try:
        return _check_one_inner(p, base, key, protocol)
    except Exception as e:
        return {"id": p.get("id", "?"), "name": p.get("name", "?"), "ok": False,
                "status": "error", "detail": "检测异常：" + _short(e)}


def _check_one_inner(p, base, key, protocol):
    pid = p["id"]; name = p["name"]
    if not base:
        return {"id": pid, "name": name, "ok": None, "status": "unconfigured", "detail": "需填地址与 Key"}
    if not key:
        if p.get("nokey"):
            key = ""  # 免 Key 服务商：以空 Key 直接探测
        else:
            return {"id": pid, "name": name, "ok": None, "status": "unconfigured", "detail": "未填 Key"}
    model = (p.get("models") or [""])[0] or ""
    if protocol == "anthropic":
        # 有的 Anthropic 兼容站其 WAF 只放行官方同步 SDK，
        # 用 urllib 探测会被拒（401 unauthorized client detected），故改用 SDK。
        try:
            anthropic = _anthropic_sdk()
        except Exception as e:
            return {"id": pid, "name": name, "ok": None, "status": "unconfigured", "detail": _short(e)}
        try:
            client = anthropic.Anthropic(api_key=key, base_url=base)
            msg = client.messages.create(model=model or "claude-opus-4-6", max_tokens=4,
                                         messages=[{"role": "user", "content": "ping"}])
            content = "".join((getattr(b, "text", "") or "")
                              for b in (msg.content or [])
                              if getattr(b, "type", "") == "text").strip()
            low = content.lower()
            bad_words = ("api key", "budget", "quota", "insufficient", "payment",
                         "unauthorized", "invalid key", "rate limit", "too many requests")
            hit = next((w for w in bad_words if w in low), None)
            if hit:
                return {"id": pid, "name": name, "ok": False, "status": "error",
                        "detail": _short(content) or ("上游报错：" + hit)}
            return {"id": pid, "name": name, "ok": True, "status": "ok", "detail": "可用"}
        except Exception as e:
            em = str(e)
            if "无可用渠道" in em or "no available channel" in em or "overloaded" in em.lower():
                # 能收到这条说明 WAF 已放行、key 有效，属服务方容量问题
                return {"id": pid, "name": name, "ok": False, "status": "error",
                        "detail": "上游无可用渠道（key 与配置已通过，属服务方容量问题）"}
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": _short(em)}
    if model:
        # 真实小调用（max_tokens=5），最能反映“能不能聊”
        try:
            url = base.rstrip("/") + "/chat/completions"
            payload = {"model": model, "max_tokens": 64, "stream": False,
                       "messages": [{"role": "user", "content": "ping"}]}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                         "User-Agent": UA, "Accept-Encoding": "identity"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read().decode("utf-8", "ignore")
            try:
                obj = json.loads(data)
            except Exception:
                return {"id": pid, "name": name, "ok": True, "status": "ok", "detail": "可用（返回非 JSON）"}
            if obj.get("error"):
                em = obj["error"]
                em = em.get("message", em) if isinstance(em, dict) else em
                return {"id": pid, "name": name, "ok": False, "status": "error", "detail": _short(em)}
            # 有的上游是 HTTP 200 + 私有错误体（如 MiniMax 渠道的 base_resp），必须当失败
            br = obj.get("base_resp")
            if isinstance(br, dict) and br.get("status_code") not in (0, None):
                return {"id": pid, "name": name, "ok": False, "status": "error",
                        "detail": _short(br.get("status_msg") or ("上游错误码 " + str(br.get("status_code"))))}
            # 部分上游把错误塞进正文字段（如额度耗尽），只判 error 字段会误报“可用”
            ch = obj.get("choices") or []
            msg0 = (ch[0].get("message") or {}) if ch else {}
            content = (msg0.get("content") or "").strip()
            # 推理模型在 token 上限很小时只吐 reasoning_content，不能据此判死
            reasoning = (msg0.get("reasoning_content") or "").strip()
            low = (content or reasoning).lower()
            bad_words = ("api key", "budget", "quota", "insufficient", "payment",
                         "unauthorized", "invalid key", "rate limit", "too many requests")
            hit = next((w for w in bad_words if w in low), None)
            if hit:
                return {"id": pid, "name": name, "ok": False, "status": "error",
                        "detail": _short(content or reasoning) or ("上游报错：" + hit)}
            if content:
                return {"id": pid, "name": name, "ok": True, "status": "ok", "detail": "可用"}
            if reasoning:
                return {"id": pid, "name": name, "ok": True, "status": "ok",
                        "detail": "可用（推理模型，探测限额内只返回了推理过程）"}
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": "上游返回空内容"}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": f"{e.code} {_short(detail)}"}
        except Exception as e:
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": _short(e)}
    else:
        # 无内置模型名：退化为 GET /models 校验 key + 连通
        try:
            url = base.rstrip("/") + "/models"
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + key, "User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read(64)
            return {"id": pid, "name": name, "ok": True, "status": "ok", "detail": "地址/Key 可达（未验模型）"}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": f"{e.code} {_short(detail)}"}
        except Exception as e:
            return {"id": pid, "name": name, "ok": False, "status": "error", "detail": _short(e)}


def _has_path(base):
    """地址里只有域名没有路径（如 https://api.example.com）"""
    return "/" in base.split("//", 1)[-1]


def iter_sse(resp):
    for raw in resp:
        line = raw.decode("utf-8", "ignore").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[5:].strip())
        except Exception:
            continue


def with_image(messages, image_url, proto="openai"):
    """把最后一条 user 消息换成多模态格式（模型配置栏「图片地址」开关）。

    只对支持视觉的模型有效；上游不认时报错，由故障转移接手。
    """
    if not image_url or not messages:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") != "user":
            continue
        txt = m.get("content")
        if isinstance(txt, list):
            break                      # 已经是数组，不要重复包
        txt = txt or ""
        if proto == "openai":
            m["content"] = [{"type": "text", "text": txt},
                            {"type": "image_url", "image_url": {"url": image_url}}]
        else:
            m["content"] = [{"type": "text", "text": txt},
                            {"type": "image", "source": {"type": "url", "url": image_url}}]
        break
    return out


def apply_persona(messages, persona):
    """网关忽略 system 字段，改用 user/assistant 预置轮把角色设定塞进去。"""
    if not persona:
        return messages
    seed = [
        {"role": "user", "content": persona + "\n\n请记住上面这个设定，之后的对话都按它来回答。"},
        {"role": "assistant", "content": "好的，我明白了，接下来会一直按这个设定回答。"},
    ]
    return seed + messages


class ClientGone(Exception):
    """客户端已断开，不必再写 SSE，也不必再等上游。"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        # 客户端强断（点「停止」）后，keep-alive 的下一次读会抛 ConnectionReset，
        # socketserver 默认会在窗口里甩一整段堆栈。这里静默收掉。
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _sse(self, obj):
        # 客户端已断开（点「停止」或关页面）时写会抛 BrokenPipe / ConnectionReset。
        # 转成专门的异常，让上层能立刻收掉上游请求，而不是继续跑完 300 秒。
        try:
            data = ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise ClientGone(str(e) or "client disconnected")

    def _end(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = (PAGE.replace("__MODELS__", json.dumps([]))
                        .replace("__PERSONAS__", json.dumps(PERSONAS))
                        .replace("__PROVIDERS__", json.dumps(public_providers()))
                        .encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif path == "/models":
            self._json(200, {"models": []})
        elif path == "/sessions":
            self._json(200, {"sessions": list_sessions()})
        elif path == "/session":
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("id") or [""])[0]
            data = load_session(sid)
            self._json(200 if data else 404, data or {"error": "not found"})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path != "/session":
            return self._json(404, {"error": "not found"})
        sid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
        p = _hist_path(sid)
        if os.path.isfile(p):
            os.remove(p)
            log(f"  session deleted: {sid}")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/save":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n)) if n else {}
            except Exception as e:
                return self._json(400, {"error": f"bad json: {e}"})
            sid = save_session(payload)
            return self._json(200, {"ok": True, "id": sid})
        if path == "/list_models":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n)) if n else {}
            except Exception as e:
                return self._json(400, {"error": f"bad json: {e}"})
            base = (payload.get("base") or "").strip().rstrip("/")
            protocol = (payload.get("protocol") or "openai").strip()
            prov_id = (payload.get("prov") or "").strip()
            key = prov_key(prov_id, payload.get("key"))
            if not base:
                return self._json(200, {"error": "先填 API 地址"})
            extra = None
            if protocol == "anthropic":
                if not base.endswith("/v1"):
                    base += "/v1"
                extra = {"x-api-key": key,
                         "anthropic-version": "2023-06-01",
                         "Accept": "application/json"}
            elif not _has_path(base):
                base += "/v1"
            try:
                with open_api(base, key, "/models", extra=extra) as r:
                    data = json.loads(r.read().decode("utf-8"))
                models = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
                return self._json(200, {"error": f"上游 {e.code}" + (f"：{detail}" if detail else "")})
            except Exception as e:
                return self._json(200, {"error": str(e)})
            return self._json(200, {"models": models})
        if path == "/check_providers":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n)) if n else {}
            except Exception:
                payload = {}
            overrides = (payload.get("overrides") or {}) if isinstance(payload, dict) else {}
            force = bool(payload.get("force"))
            now = time.time()
            age = int(now - _PROV_CACHE["ts"])
            if not force and _PROV_CACHE["results"] and age < PROV_CACHE_TTL:
                return self._json(200, {"results": _PROV_CACHE["results"],
                                        "cached": True, "age": age})
            from concurrent.futures import ThreadPoolExecutor
            def worker(p):
                ov = overrides.get(p["id"]) or {}
                base = (ov.get("base") or p.get("base") or "").strip().rstrip("/")
                key = prov_key(p["id"], ov.get("key"))
                return _check_one(p, base, key, p.get("protocol", "openai"))
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(worker, PROVIDERS))
            _PROV_CACHE["ts"] = now
            _PROV_CACHE["results"] = results
            return self._json(200, {"results": results, "cached": False, "age": 0})
        if path != "/chat":
            return self._json(404, {"error": "not found"})

        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n)) if n else {}
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})

        messages = payload.get("messages") or []
        model = (payload.get("model") or "").strip()
        persona = (payload.get("persona") or "").strip()
        try:
            temperature = float(payload.get("temperature", 0.7))
        except (TypeError, ValueError):
            temperature = 0.7
        temperature = max(0.0, min(1.0, temperature))
        question = messages[-1]["content"] if messages else ""
        adv = payload.get("params") or {}
        if not isinstance(adv, dict):
            adv = {}
        api = payload.get("api") or {}
        if not isinstance(api, dict):
            api = {}
        # 图片地址（模型配置栏「图片地址」开关）：只放行 http(s)，别把 data: 之类透给上游
        image = (payload.get("image") or "").strip()
        if image and not re.match(r"^https?://", image):
            image = ""
        # 顺手把检测结果缓存续上（不阻塞本次请求），下次打开面板能立刻出结果
        refresh_prov_cache_async()
        api_base = (api.get("base") or "").strip().rstrip("/")
        api_key = (api.get("key") or "").strip()
        api_proto = "openai" if (api.get("protocol") == "openai" and api_base) else "anthropic"
        if api_proto == "openai" and not _has_path(api_base):
            api_base += "/v1"
        # 解析服务商：key 不再随页面下发，前端留空时由服务端取内置 key
        api_id = api.get("prov") or ""
        prov_nokey = False
        for _p in PROVIDERS:
            if _p["id"] == api_id:
                prov_nokey = bool(_p.get("nokey"))
                if not api_key:
                    api_key = (_p.get("key") or "").strip()
                break

        def _num(key, lo, hi):
            try:
                v = float(adv[key])
            except (KeyError, TypeError, ValueError):
                return None
            return max(lo, min(hi, v))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(text):
            self._sse({"type": "status", "text": text})

        try:
            if not model:
                raise RuntimeError("没填模型名（在顶部「模型名」框里填，或点「拉取模型列表」）")

            emit("思考中…")
            final_messages = messages

            def attempt(base, key, proto, mdl, nokey):
                if proto == "openai":
                    if not key and not nokey:
                        raise RuntimeError("该 API 没填 Key（在「API 设置」里填上；公益免 Key 站无需填写）")
                    if not (payload.get("model") or "").strip():
                        raise RuntimeError("没填模型名（在顶部模型框里填）")
                    body = {"model": mdl, "stream": True,
                            "messages": apply_persona(
                                with_image(final_messages, image, "openai"), persona)}
                    if temperature is not None:
                        body["temperature"] = temperature
                    v = _num("max_tokens", 64, 32000)
                    if v is not None:
                        body["max_tokens"] = int(v)
                    for k, lo, hi in (("top_p", 0, 1),
                                      ("frequency_penalty", -2, 2),
                                      ("presence_penalty", -2, 2)):
                        v = _num(k, lo, hi)
                        if v is not None:
                            body[k] = v
                    v = _num("seed", 0, 100000)
                    if v is not None:
                        body["seed"] = int(v)
                    stop = adv.get("stop")
                    if isinstance(stop, list) and stop:
                        body["stop"] = [str(s)[:60] for s in stop[:4]]
                    with open_api(base, key, "/chat/completions", payload=body) as resp:
                        # 部分上游 stream=true 仍返回单条 JSON；同时兼容标准 SSE
                        raw = resp.read()
                        text = raw.decode("utf-8", "ignore")
                        if "data:" in text:
                            for line in text.splitlines():
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    continue
                                try:
                                    evt = json.loads(data)
                                except Exception:
                                    continue
                                if evt.get("error"):
                                    err = evt["error"]
                                    raise RuntimeError(err.get("message", str(err)))
                                choices = evt.get("choices") or [{}]
                                delta_text = (choices[0].get("delta") or {}).get("content") or ""
                                if delta_text:
                                    self._sse({"type": "delta", "text": delta_text})
                        else:
                            try:
                                obj = json.loads(text)
                            except Exception:
                                raise RuntimeError("上游返回无法解析：" + text[:200])
                            if obj.get("error"):
                                err = obj["error"]
                                raise RuntimeError(err.get("message", str(err)))
                            choices = obj.get("choices") or []
                            if choices:
                                msg_text = (choices[0].get("message") or {}).get("content") or ""
                                if msg_text:
                                    self._sse({"type": "delta", "text": msg_text})
                elif base:
                    # Anthropic 兼容站：部分站的 WAF 只放行官方 anthropic 同步 SDK 的
                    # TLS 握手（见 _anthropic_sdk 的说明），urllib / 异步 SDK 会被拒。
                    if not key:
                        raise RuntimeError("自定义 Anthropic 服务商没填 Key（在「模型配置」里填上）")
                    anthropic = _anthropic_sdk()
                    # 参数名是 api_key，写成 key 会被 SDK 直接拒（unexpected keyword argument）。
                    client = anthropic.Anthropic(api_key=key, base_url=base,
                                                 timeout=TIMEOUT, max_retries=MAX_RETRY - 1)
                    kw = {"model": mdl,
                          "max_tokens": int(_num("max_tokens", 64, 32000) or 4096),
                          "messages": apply_persona(
                              with_image(final_messages, image, "anthropic"), persona)}
                    # 新版 SDK 的 messages.create 不再接受 temperature/top_p/top_k，
                    # 需经 extra_body 透传，否则直接 TypeError
                    extra = {}
                    if temperature is not None:
                        extra["temperature"] = temperature
                    v = _num("top_p", 0, 1)
                    if v is not None:
                        extra["top_p"] = v
                    v = _num("top_k", 0, 100000)
                    if v is not None:
                        extra["top_k"] = int(v)
                    if extra:
                        kw["extra_body"] = extra
                    stop = adv.get("stop")
                    if isinstance(stop, list) and stop:
                        kw["stop_sequences"] = [str(s)[:60] for s in stop[:4]]
                    msg = client.messages.create(**kw)
                    text = "".join((getattr(b, "text", "") or "")
                                   for b in (msg.content or [])
                                   if getattr(b, "type", "") == "text")
                    if text:
                        self._sse({"type": "delta", "text": text})
                else:
                    raise RuntimeError(
                        "没填 API 地址：在「API 设置」里挑一个服务商，或手填地址与 Key")

            # 出错直接把错误交给前端展示，不再静默换别的服务商
            attempt(api_base, api_key, api_proto, model, prov_nokey)

        except ClientGone:
            # 用户点了「停止」或关了页面：不写任何东西，直接收工
            log("  /chat 客户端断开，已中止")
            return
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            msg = f"上游 {e.code}" + (f"：{detail}" if detail else "")
            log(f"  /chat ERROR {msg}")
            self._sse({"type": "error", "message": msg})
            return self._end()
        except Exception as e:
            log(f"  /chat ERROR {e}")
            self._sse({"type": "error", "message": str(e)})
            return self._end()

        self._sse({"type": "done"})
        self._end()
        log(f"  /chat ok model={model} temp={temperature} "
            f"persona={bool(persona)} adv={[k for k in adv]} turns={len(messages)}")

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}/"
    log(f"本地聊天已启动: {url}")
    log(f"  已配服务商: {', '.join(p['name'] for p in PROVIDERS)}")
    log("  关闭这个窗口 = 停止服务。")

    # 启动就把服务商可用性探一遍（后台，不挡服务），面板打开即可见结果
    refresh_prov_cache_async()

    if args.open:
        import webbrowser
        webbrowser.open(url)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
