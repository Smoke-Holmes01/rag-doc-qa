# 实际验证记录

本次交付在 Windows PowerShell 和 Docker Desktop Linux 引擎完成以下验证。详细时间戳为北京时间（+08:00），见 JSON 报告。没有真实模型 Key，没有真实模型调用，也没有人类语义核验。

## 工程验证

| 命令/检查 | 实际结果 |
| --- | --- |
| `git init -b main` | 新建本地仓库，原 `AGENTS.md` 保留 |
| 独立 Python 环境 | Python 3.11.16，项目内 `.runtime` 与 `.venv`，没有改动系统默认 3.13 |
| 锁定依赖安装 | Windows uv sync 和 Linux 镜像 `pip install --require-hashes -r requirements.txt` 成功 |
| `python -m pip check` | `No broken requirements found.` |
| `python -m pytest` | **111 passed, 0 failed, 0 skipped，1 条依赖弃用提示；最终一次 2.58 秒** |
| `python -m scripts.ingest` | mock，6 份文档、22 个片段、384 维；保存后重新加载验证 |
| `python -m scripts.evaluate --mode mock` | 15 条实际执行，流程完成；所有语义状态 pending_review |
| `python -m scripts.evaluate --mode live` | 退出码 **1**，CONFIG_INVALID，0 条模型问答；未回退 mock |
| `python -m scripts.demo` | 真实回环 HTTP 的正常问答、资料不足、问题诱导、语料变化要求重建，以及缺索引，5 项分支完成 |
| `python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log` | Windows 本地启动成功；健康检查、查询、OpenAPI 返回 200；空白问题返回 422；验证后关闭 |

111 项测试包含：文档排序/过滤、UTF-8 BOM 和中文、越界来源、标题段落与长段重叠、数值/维度错误、稳定排序、完整上下文预算、产物及 manifest 损坏、语料变化、模型身份和切分变化、失败发布保留旧指针、严格模型输出、未知/未发送引用、统一错误和脱敏、资源关闭、查询只嵌入问题、模式隔离、报告生成。真实 HTTP 传输在 pytest 中被禁止；Windows asyncio 内部回环 socketpair 不属于外部调用。

依赖提示是安装版本的 Starlette 推荐 TestClient 使用 httpx2。当前任务书明确使用 httpx，受控测试在已锁定版本上成功，没有隐去提示或假报零警告。

直接依赖实装版本：FastAPI 0.143.1、Pydantic 2.14.1、pydantic-settings 2.16.0、httpx 0.28.1、NumPy 2.4.6、pytest 9.1.1、Uvicorn 0.54.0。传递依赖及哈希以锁定文件为准，不依赖这些版本号的口头描述来重建环境。

## Docker 验证

实际执行 `docker compose build`、`docker compose run --rm ingest python -m scripts.ingest` 和 `docker compose up -d api`，并对最终修改后的镜像再次构建/启动核验。`-d` 用于在同一验收流程内继续发送请求，README 的前台命令使用同一服务配置。

- 初次未构建索引时 API 容器可以启动，healthz 的 index_ready=false，查询为 503 INDEX_NOT_READY。
- ingest 以 UID 10001 成功构建索引，额外写入探针成功并删除探针文件。
- API 同为 UID 10001，语料和索引挂载均为只读。实际写入索引探针被文件系统以 **errno 30 / EROFS** 拒绝。
- 容器 `/app/.env` 不存在，Key 通过运行环境而非镜像文件传入；本次 Key 为空。
- 端口只绑定宿主机 `127.0.0.1:8001`，容器进程监听 `0.0.0.0:8001`。
- 构建后健康检查 index_ready=true，正常问答和资料不足均按契约返回，容器内 15 条 mock 评测完成。
- 验收后执行 `docker compose down`，停止项目容器并移除网络，保留索引卷。没有停止用户的 Docker 引擎或删除其他项目数据。

详细证据：[容器验证 JSON](../reports/mock/container_verification.json)。该文件只保存挂载目的地/读写属性，不包含宿主机个人路径或完整容器环境。

## 评测解释和未验证项

[mock 报告](../reports/mock/evaluation.json) 的预期依据完整覆盖为 **8/12**，匹配规则是“指定文件、章节、必要原文全部进入实际上下文”。三条无依据问题不进入分母。该结果是哈希测试替身的流程输出，不能代表 BGE-M3 检索能力。

F06、C02、I01、I02 未完整提供预期依据；U01、U03 的确定性摘录没有按预期拒答。失败被保留，未将当前输出反写成标准答案。详见 [失败分析](design.md)。

- **工程流程完成**：上述离线测试、mock 索引/API/评测、真实本机 HTTP 与 mock Docker 路径已验证。
- **真实模型未验证**：硅基流动 HTTP 映射有官方文档依据和 MockTransport 测试，但无 Key，未构建 live 向量，未调用真实聊天模型；live Docker 同样未验证。
- **语义质量待人工核验**：全部回答/拒答/引用支持关系仍为 pending_review，只有 Agent 辅助检查，不是人类确认。未计算或宣传回答准确率、可靠拒答率或抗攻击率。

固定预期依据由 Agent 先于问答运行编写，本身也需要用户逐条核对原文。补齐 live 配置后，应先构建 live 索引，再运行 15 条评测，并填写逐条核验表，不能据当前 mock 验收把 live 项目勾成通过。
