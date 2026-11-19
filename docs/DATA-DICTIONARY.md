# 授权定义

资源orders与订单等价，customers与客户等价。principal为主体文本，不支持模糊主体。partition为YYYY-MM；分区授权[start,end)不含end月。授权时间也为[start,end)，到期时刻立即失效。

每条授权包含主体、资源、生效窗、分区窗和allow/deny。全部条件必须同时匹配。拒绝优先，多个允许不能覆盖任何显式拒绝；无匹配默认拒绝。匹配规则ID按字典序稳定排序，冲突显示全部允许与拒绝证据。

observed为真实访问执行结果allowed或blocked；reported为上游0/1判定，留作证据。expected为实际授权复算，分区模块重建reported为执行结果便于二值对账。越权放行和误拦截分别计数，禁止用相互抵消的总数代替逐条核查。

业务ID和超大整数按文本保形；Decimal参与数量金额计算，对外序列化为字符串。哨兵集合定义在app/values.py，缺失值不能当零。所有时间带UTC偏移，先转UTC比较，业务日按指定地区划分。区间统一[start,end)。重复业务键保留第一条、完全相同行计重复；值冲突行隔离且保留原值。空文件、全空白、仅表头分开拒绝。

运行清单包括输入sha256、规则版本、参数、生成时刻与耗时。源行编号以CSV解析器的物理结束行号表示，引号内换行不增加记录数。输入超过4MB返回413，非法表头和类型返回422，失效版本复核返回409。

# 续跑到期裁定

`parameters.resume`描述一次跨分区作业的断点续跑，字段：`job_id`、`checkpoint_id`、`checkpoint_digest`（sha256）、`completed_before`（断点前已完成行数）、`partitions`（断点覆盖的分区，YYYY-MM）、`started_at`/`failed_at`/`resumed_at`（带偏移时间）、`original_grant_id`。

裁定规则：续跑按 `resumed_at` 作为访问时刻重新匹配授权，原授权到期即失效，不随作业延续；业务方“同一作业沿用原授权”的主张只记入证据，不参与放行。裁定按 `(job_id, checkpoint_id, checkpoint_digest)` 唯一确定，同一断点重试沿用首次裁定的 `resumed_at` 且不新增裁定，重试输入或授权版本与首次证据不一致时标记 `CHECKPOINT_EVIDENCE_MISMATCH`。任一字段缺失/非法、行分区超出断点声明，或续跑时刻缺失，都记入 `issues`，该批次不得封存或导出证据（409），但旧版本与原证据保留。