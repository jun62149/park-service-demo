# 园区企服报修智能运营平台 · 本地 Demo

用于演示企服与工程协作的 Flask + SQLite 报修闭环：报修创建、企服分派/改派、指定工程接单、照片与处理说明、企服验收关闭/退回、事件时间线、角色工作台和管理指标。支持辅助解析、规则降级、站内时间提醒，以及可选飞书企业自建应用机器人主动通知。

**工单业务在本地 Web 中完成。飞书 App Bot 仅发送通知，不是飞书原生工单系统，不支持在飞书端直接创建工单、消息报修或卡片远程操作。** 全部身份、数据和图片用于虚构演示；角色切换不是生产认证。仅用于 Windows 回环地址本地 Demo，不应直接公网部署。

## Windows 安装与离线启动

已验证 Python 3.10.11。先安装 Python 并使 `python` 可用，解压 ZIP 后在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt
$env:AI_PROVIDER = 'rules'
$env:FEISHU_ENABLED = 'false'
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

打开 [本地工作台](http://127.0.0.1:5000)。上述方式不加载本机配置文件，不需要飞书凭据、DeepSeek Key 或 Ollama。首次安装依赖需要网络；安装完成后的规则/手填演示可离线运行。停止服务按 Ctrl+C。

端口占用时使用 `-Port 5096`，然后打开 `http://127.0.0.1:5096`，不要关闭不属于本项目的服务。已安装依赖也可直接执行 `.\.venv\Scripts\python.exe run.py`。双击 `start.cmd` 会安装固定依赖并加载本机配置；已有真实配置时请先检查开关，离线演示优先使用上面的显式命令。

首次启动创建6个虚构身份和1条待分派种子；再次启动保留 SQLite 数据。运行DB、上传照片和会话签名位于被忽略的 `instance/`，不能提交、分享或打包。交付 ZIP 不含这些运行数据、.env、.env.feishu 或虚拟环境。

## 5–8 分钟演示

完整演示脚本由交付者放在源码 ZIP 外另行提供。最短路径：

1. 报修人手填，或在“辅助解析报修”保存虚构描述、选择“不出站，使用规则降级”，人工纠错后创建。
2. 切企服，确认普通/加急/特急优先级，指定工程甲分派；待接单阶段支持说明原因后改派。
3. 切指定工程接单，填写处理说明并上传 `tests/fixtures/demo-placeholder.png`。
4. 切企服检查照片/说明，验收关闭；也可填写原因退回，由工程重新处理并提交本轮新凭证。
5. 查看各轮凭证、分派历史和事件时间线；管理者看总量、六态分布、演示关闭率、当前逾期及特急待响应。

六态为待分派、待接单、处理中、待企服验收、已退回、已关闭；只有企服可关闭，终态不能修改。工程只能操作本人当前负责的合法工单。后端检查身份、CSRF、版本及照片凭证，不能通过伪造请求体角色绕过。

照片支持1–3张 PNG/JPEG，单张最多5 MiB、请求最多16 MiB、最多2000万像素；检查实际内容并重编码加演示水印。附件经权限路由访问，不能直接从静态目录取文件。

特急从本轮分派/站内推送起满300秒未接单提示人工电话跟进，不拨号、不发短信。截止时间由企服手填，可空；为空明确显示无法判断超期，不推导园区SLA。有截止且当前时间大于截止、工单未关闭才计当前逾期；关闭后仅记历史超期。演示时钟可向前快进，隐私确认有效期仍使用真实单调时钟。

企服可进入“演示数据重置”，输入“重置演示数据”二次确认：业务记录和时钟同步重置为3条虚构样本、旧会话失效、旧通知队列清除；仅清理登记的受控照片，不递归清理孤立文件。飞书身份映射是本机配置，不随演示重置删除。

## AI 配置与真实验证状态

应用只读后端进程环境，不自动加载.env。`.env.example` 是变量清单，Key为空；要加载本机配置须显式使用 `start.ps1 -LoadLocalEnv`。默认 Provider 是 DeepSeek、模型 `deepseek-flash`，显式关闭Thinking、非流式JSON Output并做本地Schema验证；缺Key、超时或异常直接规则降级，界面显示真实来源/原因。调用总预算默认/最大20秒，无隐式重试，不自动转发Ollama。

云调用前必须由后端生成脱敏预览、绑定确认并一次性消费；修改、过期、跨会话或载荷替换会失效。自动脱敏可能遗漏，无法确认时选择不出站。模型只返回建议，不派单、不验收、不修改工单状态；最终优先级由企服确认。

可选本地Ollama需显式配置 `AI_PROVIDER=ollama`、`OLLAMA_MODEL=qwen3:14b`，地址默认回环11434；纯规则使用 `AI_PROVIDER=rules`。DeepSeek已有一次虚构数据真实成功记录，实际服务端Thinking模式未独立证实；Ollama真实成功解析仍未验证。交付整理不重复真实模型调用。

## 飞书 App Bot 配置

无凭据时保持 `FEISHU_ENABLED=false`，全部工单主流程仍可使用。需要真实主动通知时：

1. 创建企业自建应用，添加并开启“机器人”能力。
2. 在权限管理开通应用身份 **获取与发送单聊、群组消息 `im:message`**。这是本项目配置清单使用的基础权限；官方也支持更窄的 **以应用的身份发消息 `im:message:send_as_bot`** 作为本发送接口的替代，不必同时申请两项。不需要全量通讯录、群历史、审批、文档或管理员权限。
3. 将测试工程和企服加入应用可用范围，发布应用及权限并完成审核。测试版需在对应测试企业/账号中使用。
4. 暂不配置事件与回调；没有订阅im.message.receive_v1，也没有消息接收或远程工单入口。

参考：[官方发送消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create)。后端通过自建应用token接口取得tenant_access_token，以 `receive_id_type=open_id` 发送单聊文本；不使用群自定义机器人Webhook。

在项目根目录配置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\configure_feishu.ps1
```

隐藏输入App ID、App Secret、工程甲open_id，可选企服open_id；保存到受忽略的.env.feishu和instance/feishu_bindings.json，不发送消息。已有.env凭据只补映射时追加 `-BindingsOnly`，不会覆盖凭据。不要把Secret、token、真实ID贴入聊天、截图、源码或日志。

后端变量为 `FEISHU_ENABLED=false`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_TIMEOUT_SECONDS=5`；`FEISHU_BINDINGS_FILE` 默认指向运行目录映射文件。只通过安全本机文件或后端环境注入，不写前端。

映射文件结构示例（仅虚构占位值，不能直接用于真实发送）：

```json
{
  "3": {"open_id": "ou_FAKE_ENGINEER_A_REPLACE_LOCALLY", "enabled": false},
  "2": {"open_id": "ou_FAKE_SERVICE_REPLACE_LOCALLY", "enabled": false}
}
```

用户3是工程甲、2是企服；open_id必须来自当前应用，不能用姓名或其他应用的ID替代。普通前端不能指定收件者；缺映射只记unconfigured，禁用为disabled，不发给其他人或默认群。更改映射需停止发送器、修改本机文件并重启；历史失败/不确定消息不会自动重发。

明确决定启用真实自动通知后，检查有效本机配置中的FEISHU_ENABLED=true，再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -LoadLocalEnv -Port 5085
```

加载顺序为.env之后.env.feishu，后者优先；配置助手完整模式会写入true，但只有显式加载/启动才生效。每轮评估提醒并处理至多一项，轮间1秒；仅支持单进程本地服务，禁止调试重载器/多WSGI worker或旧版共用此运行DB。停止按Ctrl+C。启用时企服/工程可通过页面“通知状态（API记录）”查看发送状态，工程只能看自己的通知记录。

工单成功事务只入队，提交后才请求外网；失败不回滚已成功的工单。明确限流/连接超时最多3次，按真实时间退避5/30秒；消息读取超时、连接中断或5xx标uncertain，不自动重试，不能承诺严格一次送达。重启sending转uncertain；pending按原退避恢复。超时5秒是单次HTTP连接/读取设置，token与发消息是两个请求，不能解释为严格墙钟总预算。

消息只含虚构工单编号、安全位置/故障类别、状态、优先级和虚构责任人，不发送联系人、手机号、完整原文、处理/退回自由文本、附件或AI原文。重置与发送串行化，等待外网期间不占SQLite写事务，完成写回再查generation；已提交远端的消息无法撤回，可能稍后显示。

| 通知 | 当前验证状态 |
|---|---|
| 分派/改派→指定工程 | 核心分派已真实发送且用户确认客户端收到；改派经本地测试 |
| 完工→本轮分派企服 | 已真实发送且用户确认客户端收到 |
| 退回→当前工程 | 仅本地HTTP Stub/自动化验证，未真实收件验证 |
| 特急满300秒未响应→本轮企服人工跟进 | 仅本地HTTP Stub/自动化验证，未真实收件验证 |
| 有手工截止且超期→工程/待验收企服 | 仅本地HTTP Stub/自动化验证，未真实收件验证 |

`sent` 只表示API接受，客户端是否收到另行确认。真实验证记录由交付者在源码 ZIP 外另行提供，不把Stub当作真实飞书结果。本轮交付整理不再次发送消息；`-FeishuOnce` 是会真实发送的独立测试选项，不属于离线安装步骤。

## 测试与交付材料

源码仓库保留 tests/ 下的261项测试；原本地262项联合测试另含 docs/ 下1项历史并发审计用例，该目录不提交。包内测试使用隔离临时DB/上传目录：

```powershell
# 仅下载源码 ZIP 时需要 git init；已 git clone 的仓库无需再次初始化。
git init
New-Item -ItemType Directory -Force -Path instance | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --noconftest -p tests.conftest tests --basetemp=instance/pytest-local
```

保持离线服务运行，在另一终端执行核心HTTP冒烟（将创建虚构数据）：

```powershell
.\.venv\Scripts\python.exe scripts/http_smoke.py --base http://127.0.0.1:5000 --output instance/http-smoke-local.json
```

本源码仓库不提交 docs/（或 doc/）目录，不含调研报告、FDE方案、阶段报告、证据、演示脚本或交付清单。本 README 是仓库内唯一的说明文档；本地历史文档不属于远程源码交付内容。报告与源码 ZIP 的外层汇总由交付者另行整理。不要把本机 .env、instance、运行数据库或实际映射放入外层交付材料。

原有联合测试、五类通知HTTP Stub及真实收件验证的范围，以另行提供的验收报告为准；测试通过不等于本轮浏览器验收，也不等于其余三类真实飞书收件。

未验证第二台机器、生产身份认证、公网部署、依赖全部安全漏洞或全部真实通知收件。消息延迟、断电/强杀、文件系统与数据库非原子以及本机文件权限仍是Demo限制。没有真实园区样本支持业务提效百分比或生产SLA结论。
