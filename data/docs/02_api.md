# 星桥文档服务：API 约定

本文件属于自编的星桥 v1.0 演示资料，与任何真实厂商产品无关。

## 查询输入

POST /v1/query 接受 question 和可省略的 top_k。question 必须为非空白字符串，默认最多 1000 个字符；top_k 默认 4，最大 8，必须为正整数。

客户端不能提交模型名、提示词、任意上下文或文件路径。多余字段、字符串形式的数量及布尔数量均返回 422 INVALID_REQUEST。

## 回答和引用

成功结果包含 request_id、mode、status、answer、claims 和 citations。status 为 answered 时，每条结论都必须引用本次上下文中的来源 ID。文件名、章节和原文由服务按来源 ID 回填。

资料不足使用 insufficient_evidence，HTTP 状态为 200，claims 和 citations 都为空。上游超时或无效 JSON 属于运行错误，不得伪装成正常资料不足。

## 健康检查

GET /healthz 不访问远端模型，只报告进程状态、运行模式、本地索引是否就绪及不可用原因。即使缺少索引，进程仍可启动并返回 index_ready=false。

index_ready=true 不代表模型供应商当前一定可用。调用方需要区分进程存活、索引就绪和远端调用结果。
