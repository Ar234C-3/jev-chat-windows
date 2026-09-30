# HANDOFF — jev-chat-windows 改造交接

> 交接时间：2026-09-30 · 分支 `feat/all-features`（= 分支3，`32cfffd`）
> 配套：项目记忆 `MEMORY.md`（git 流程 / 构建路径 / GCM 参数等常设规矩在那里，此处不重复）

## 任务目标

对 [jev-chat-windows](https://github.com/jev-chat/jev-chat-windows)（本地 OCR + 悬浮窗的聊天回复辅助）做一轮完整增强：

1. **判断模型扩展**：原判断只支持 OpenRouter / TypeSafe（Decisions API），要接入**小米 MiMo** 及任意 **OpenAI 兼容**接口
2. **耗时优化**：用户报告生成回复 40~60s，定位瓶颈并落地优化
3. **新增 QQ 识别**：在微信、KakaoTalk 之外支持 QQ 聊天窗口
4. **工程规范**：分支 + PR 的版本控制、README 全面对齐、以 v1.0.0 正式发布

## 已完成的部分

### 判断模型扩展（`feat/mimo-judge` 四提交，已合并进 main=`415d378`）

- `bb61728` 判断源接入小米 MiMo 与「自定义 · OpenAI 兼容」（chat 桥接：7 道判断题折成 prompt → JSON 解析回 Decisions API 同款形状），复用 `JEV_API_KEY` 槽；同提交带入**分段计时与进度提示**、判断输出契约瘦身、JSON 收尾截断修复
- `6aa2024` 方案②：排序并入起草自评（设置开关 `self_rank`），砍掉独立排序调用
- `6735eed` 自评输出截断残骸漏进候选的三处修复（max_tokens 1600 / raw_decode 抢救 / 残骸过滤）
- `80b85c7` 失败原因安全分类进聊天记录（只记超时/HTTP状态/解析失败，绝不含对话内容）

### 耗时诊断（结论都进了项目记忆）

- ①实验证明：判断耗时与输出长短**无关**，同契约单次 10.9~32.7s 波动 = 接口/网络尾延迟；「40~60s」= 波动撞上失败重试的降级路径
- 用户随后把判断源换成 **OpenRouter + `typesafe/jev-1.13`**（专用小模型走原生 Decisions API，**1~5s**）——瓶颈根源是「拿通用大模型干判别式小活」
- 诊断设施沉淀：分段计时状态行、失败原因分类行（判断失败会写 `判断失败（37.6s · 超时），已退回盲起草老路` 这类行）

### QQ 识别（`feat/qq-support` 四提交）

- `e0cf7d3` `chatapps` 注册 QQ（`#12b7f5` 亮蓝谓词 + 7 反例像素自测）；`chat_area` 两处实测修正：标签列 1px 竖线切分（逐列扫、RGB 打包 unique、门禁保证微信不误伤）、工具栏图标行定位输入框（qq_5 y_in 1061→831、qq_4 1392→1088）；探针 `probe_qq` / `qq_colors` / `qq_boxes` / `qq_crop_ocr`
- `76a1421` 会话名改用**窗口标题**（`title_from_window`）：OCR 读不出日文名、误读成「聊天」；剥「等N个会话」未读徽标防会话键分裂
- `e1b3830` README 增补 QQ
- `c85de00` **前台焦点跟随**（点谁采谁 + 粘性 + 暂停保护 + 切窗提示）

### 文档与发布

- `32cfffd` README 全面审计：界面说明（状态栏条目）、功能、工作原理流程图、项目结构、已知限制、更新记录 → **v1.0.0**、三张截图重拍
- **v1.0.0 已发布**：https://github.com/Ar234C-3/jev-chat-windows/releases/tag/v1.0.0
  （本地按 CI 同款配方构建：版本注入 `1.0.0`、LICENSE/NOTICE 入包、zip 命名一致，171.3MB）

## 当前状态

### Git（远端）

```
main                 415d378  ← PR #1 已合并（MiMo/计时/自评/原因分类）
feat/all-features    32cfffd  ← 分支3：main 全量 + QQ + 焦点 + README终稿；PR #3 待审
feat/qq-support      c85de00  ← PR #2，内容已被分支3包含，**合并 #3 后关闭**
feat/mimo-judge      80b85c7  ← 已合并，可删
main 之外另有 tag v1.0.0 → 32cfffd
```

- 本地工作副本：`…\https-github-com-jev-chat-jev\jev-chat-windows`，在 `feat/all-features` 上，工作区干净
- 成品：`C:\Users\admin\Downloads\jev-chat-windows\` = 集成版 exe（设置保留；比 tag 早两个纯文档提交，二进制等价）
- 备份：`Downloads\jev-chat-windows-backup-20260929\`（计时版成品）、会话目录 `jev-chat-windows-src-backup-20260929\`

### 环境速查

- 构建 venv（**短路径**，PySide6 装不进深目录）：`C:\Users\admin\AppData\Local\Temp\jev-venv`——**会被系统清理，没了就按 `Temp\jev-venv` + requirements.txt + pyinstaller 重建**
- 构建源目录：`C:\Users\admin\AppData\Local\Temp\jevsrc\jev-chat-windows`（robocopy 同步后 PyInstaller；深仓库路径只当源码用）
- gh CLI：`C:\Users\admin\gh-cli\bin\gh.exe`（便携版，PAT 登录，未进 PATH）
- git 推送必须带 GCM 参数（`credential.helper=…git-credential-manager.exe`，机器上的 `helper-selector` 是悬空配置），提交用一次性 `-c user.name/email` 占位身份
- 深仓库 `.venv`：仅装了 SDK+OCR+capture（够跑 core 自测和探针），**没有 PySide6**——`import main` 要用短路径 venv

### 用户当前配置（config.json 快照）

判断 = OpenRouter `typesafe/jev-1.13`（原生 Jev，1~5s）；起草 = 自定义 `/v1` + `mimo-v2.6-flash`；`self_rank=false`（自评关、独立排序）；context=12。

## 关键决策及原因

| 决策 | 原因 |
|---|---|
| MiMo 走 **chat 桥接**而非 Decisions API | 判断侧协议是结构化 RPC（noul/choice/score），通用模型打不进；桥接保持返回形状一致，engine/降级路径零改动 |
| ① 输出契约瘦身**保留**（实验证明对耗时无效） | 无害且省 42% 输出 token；真凶是请求级波动，不在这层 |
| 判断源最终换回 **Jev 原生（OpenRouter）** | Jev 是为这 7 道题训练的小判别模型，1~5s vs 通用 flash 的十几秒；质量也更对口径 |
| ② 排序自评**做成开关**、用户实测后关掉 | 独立排序价值在判断源慢时才划算；换回 Jev 后排序也只 1~5s，独立性更值钱 |
| 焦点跟随 = **前台优先 + 粘性**，不做多开同采 | 多开要 N 份 OCR 常驻、填入目标歧义、跨软件会话名撞车——改动面远大于收益；粘性设计保证焦点在别处不乱跳 |
| QQ 会话名取**窗口标题** | 实测 OCR 读不出日文名、把头部误读成「聊天」；标题天然=当前会话，只需剥未读徽标 |
| 标签列切割加**门禁**（线左侧必须是底色主导） | 微信联系人列表结构不同，门禁让通用改动零回归 |
| git：main 只经 PR、每改版一提交一推、占位身份、绝不动用户 git config | 用户明确要求分支+PR 流程；机器无 git 身份；`helper-selector` 悬空但不擅自修 |
| probe 截图 gitignore、失败原因只记分类 | 截图含真实聊天与联系人名（仓库可能是公开的）；异常原文可能混入对话内容 |
| CI tag 触发失效 → **本地按 CI 配方发 Release** | 3 次 tag 推送零运行（dispatch 正常，判定 GitHub tag 事件抖动/fork 限制）；本地注入版本号、LICENSE 入包、zip 命名与 CI 完全一致 |

## 已知问题

**性能**

- 判断侧接口波动 10.9~32.7s（同契约同输入），属服务端/网络尾延迟，非代码可治；换 Jev 原生后已降到 1~5s
- SDK 内部重试**不可见**：曾计划在状态行显示「判断 Xs（含 N 次网络重试）」，未实施——若波动复发这是第一诊断手段

**QQ 识别（README 已知限制有完整版）**

- URL 单行蓝字对浅蓝底对比不足 → 按灰字丢（同行正文不受影响）
- 白底窗输入框锚点未精调，占位符靠「平底色」规则兜底拦下
- det 对个别短句间歇漏检：实时多帧循环自愈，单帧探针会低估
- 群窗一帧 OCR 1~3.6s（比微信重，可调 det 参数优化）
- `who_said` 判决本身审计矛盾 0 行；qq_4 大量「图片丢弃」是内容本身为卡片/截图，符合设计

**焦点跟随**

- 切窗重置去重状态 → 切过去首轮会把可见消息当新消息报一次（换来「切过去立刻有候选」）；来回切会多触发分析

**工程/环境**

- **CI 标签触发不生效**（v1.0.0 推 3 次零运行；dispatch 正常）——下次发版先观察，若仍失效需查 fork 的 Actions 限制或改用本地配方发版；dispatch 留下过一个短 SHA 版 artifact（90 天自动过期）
- Temp 构建 venv 会被系统清理（本会话被清 2 次）；深路径撞 Windows 长路径（PySide6 只能装短路径 venv）
- GitHub 连接间歇 500/reset：推送失败先重试一次再报告（已写入项目记忆）
- Read 工具图像通道曾整晚串台（返回旧附件）——已恢复，若复发可用程序化解剖替代

**流程状态**

- PR #3 待用户按其检查清单实测后合并；合并后**关闭 PR #2**（内容被包含）
- 用户改动尚未提交的：无（工作区干净）

## 下一步计划

**用户侧（合并前检查清单，来自 PR #3）**

1. exe 实测 QQ 单聊 + 群聊出候选、会话名跟随窗口标题（含未读徽标变化不分裂）
2. 微信 ↔ QQ 焦点来回切换，确认状态栏「采集窗口已切到…」与候选切换
3. 微信老流程回归（`chat_area` 改动的门禁理论无影响，需实证）
4. 通过后合并 PR #3 → 关闭 PR #2 → 可删除 `feat/mimo-judge` 分支

**观察项（v1.0.0 实测期）**

- QQ 识别质量（尤其群聊发言人挂载、灰字误伤）；判断耗时波动与「失败原因」行的真实分布
- 下次发版时重试 tag 触发，验证 CI 自动发布是否恢复

**Backlog（按价值排序）**

1. SDK 重试可见性：判断耗时行标注「含 N 次重试」（波动复发时的定位利器）
2. 方案③ 单次调用（判断+起草+排序一次问完，12~18s 目标）——对付波动最彻底，需实测判断质量
3. QQ 二期：白底窗输入框锚点精调、URL 行收录权衡、det 提速（1~3.6s → 目标 <1s）
4. 多软件同采（若真实需求出现）：N worker + 会话名加前缀 + 填入目标跟随焦点
5. 判断超时/重试参数调优（当前 timeout=30、SDK 重试2次，与降级路径的平衡）

**接手提示**：项目常设规矩（git 流程、GCM 推送参数、构建路径、判例数据）在项目记忆 `MEMORY.md`；改动前先读「已知限制」避免重复踩坑；一切网络推送失败先重试一次再报告。
