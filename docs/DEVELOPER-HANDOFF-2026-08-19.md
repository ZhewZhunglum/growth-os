# Growth OS 开发交接清单（2026-08-19）

## 交接目标

当前业务逻辑和本地源码由 Codex 提供。接手开发人员本阶段负责技术环境、独立复核和上线证据，不重新设计业务模型，也不扩大冻结的 V1 Runtime Scope。

当前本地证据：

- Python 3.12 / Django 5.2；目标数据库 PostgreSQL。
- Local/SQLite 全套 79/79 测试通过。
- `manage.py check` 通过，Migration 无漂移。
- Production 安全配置检查通过（仅配置级检查，不等于已经部署）。
- 系统只记录人工发布证明，不会调用外部平台发布。

## 先阅读

1. `README.md`
2. `docs/spec/v1-freeze-2026-08-18/00-DOGFOOD-V1-RUNTIME-FREEZE.md`
3. `docs/spec/v1-freeze-2026-08-18/01-V1-ACCEPTANCE-MATRIX.md`
4. `docs/spec/v1-freeze-2026-08-18/02-DELIVERY-EVIDENCE-AND-DEPLOYMENT-GATES.md`
5. `docs/CODE-DELIVERY-MAPPING-LOCAL-2026-08-19.md`
6. `docs/LOCAL-ACCEPTANCE-EVIDENCE-2026-08-19.md`

## 第一阶段：固定源码版本

- 将源码放入私有 Git 仓库。
- 配置真实的提交人姓名和邮箱，创建第一个 Local Candidate commit。
- 记录 commit SHA；不要把当前工作树直接称为 Production Release。
- 检查仓库中没有 `.env`、密码、Cookie、Token、真实数据库、上传内容或日志。

## 第二阶段：Docker + PostgreSQL 技术验收

在项目根目录执行：

```powershell
Copy-Item .env.example .env
# 只在本机编辑 .env，生成新的本地测试密钥和 PostgreSQL 密码；不要发到聊天或提交 Git。
docker compose build
docker compose up -d
docker compose ps
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py test
Invoke-RestMethod http://127.0.0.1:8000/health/
```

验收要求：

- 实际使用 PostgreSQL，不得悄悄回退 SQLite。
- 全部 Migration 从空 PostgreSQL 成功执行。
- 79 项或更多测试全部通过。
- PostgreSQL 上补做两个并发请求使用同一 Task UUIDv7 的真实测试：只能产生一张任务，相同内容回读原任务，不同内容必须冲突且无半成品。
- 验证外键、唯一约束、事务回滚、JSONB、重复请求幂等和 DENY 优先。
- 构建后的静态 CSS 正常；上传文件写入持久媒体卷，重启容器后仍存在。
- `docker compose down` 不得误删数据；删除 volume 只能在明确的测试清理操作中执行。
- 当前源码仅实现本地/媒体卷文件存储；腾讯云对象存储适配、凭据注入和恢复验证仍是 Staging 上线阻断项。

说明：Django 命令是 `python manage.py check`，不是 `python manage.py system check`。

## 第三阶段：准备本地 Dogfood 账号

需要四个不同的人类测试身份和一个不可登录的系统身份：

- Owner
- Operator
- Reviewer / Operations Admin
- Publisher
- Rule Evaluator（服务身份，不允许网页登录）

使用 `python manage.py bootstrap_dogfood --full-demo` 初始化。四个人类账号必须使用不同密码；密码只通过本机临时环境变量或批准的 Secret 工具注入，不写入源码、命令历史、聊天、截图或日志。执行完成后清除临时环境变量。

初始化命令会建立 PUKO 产品、封存 Profile、最新合同、强制 Policy、测试账号/环境/Capability 和最小权限，但不会自动创建 Task，也不会发布外部内容。初始 Grant 默认有效 30 天，测试前需确认仍在有效期内。

## 第四阶段：Staging

上线前由 Owner 提供或确认：

- 腾讯云账号、目标 Region 和费用边界；
- Staging/Production 域名与 DNS 控制权；
- PostgreSQL、对象存储、Secret Manager、日志/监控和备份方案；
- IAM 人员名单、MFA 和最小权限；
- HTTPS 终止位置与反向代理配置；
- 告警接收人、上线窗口、停止条件和回滚负责人。

Staging 与 Production 必须分开账号/数据库/Bucket/Secret。使用与未来 Production 相同的不可变镜像执行：

- PostgreSQL Migration、79+ 测试、`check --deploy`；
- 登录/RBAC 和权限负向测试；
- AC-01 至 AC-05；
- Task 创建、DoR 阻塞恢复、人工返工、Gate 失效重算、人工发布证明；
- HTTPS、CSRF、静态文件、媒体对象、日志和告警；
- 记录镜像 digest、commit SHA、测试时间、操作者和证据。

## 第五阶段：恢复演练

- 数据库启用 PITR；日志归档延迟建议不超过 15 分钟。
- 对象存储启用版本控制/软删除，并在 1 小时内形成独立可恢复副本。
- 在全新隔离环境真实恢复数据库、对象、配置和 Secret 引用。
- 从首次故障告警/确认开始计时，完整系统恢复必须不超过 4 小时（RTO <= 4h）。
- 恢复后的实际数据缺口必须不超过 1 小时（RPO <= 1h）。
- HTTPS、登录、数据库读写、对象读取、最小任务闭环和审计日志全部通过才算恢复完成。

## 当前明确不做

- 自动发布、自动广告或账号创建；
- 外部证据、Opportunity、ChannelPlan；
- Performance、GEO、Learning、Issue/会议治理；
- Replay/Shadow/Canary 自动规则生命周期；
- 多租户、B2B、计费、代理商功能。

发现问题时先提供：错误原文、执行命令（隐去秘密）、commit SHA、环境、日志时间、可复现步骤和预期/实际结果。不要在没有 P0 证据的情况下重写数据模型。
