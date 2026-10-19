---
name: Read Economy
description: "Use when working in the love-letter codebase to avoid redundant file reads and repeated exploration. Covers not re-reading unchanged files, batching reads, avoiding repeated directory listings and terminal status checks, and recording confirmed facts instead of re-verifying them."
applyTo: "**"
---

# 读取经济性（非重复读取）

目标：**不重复读同一份内容，不重复查同一个状态。**

## 硬性规则

1. **同一轮会话里，读过的文件不要重读**，除非：
   - 它在读取之后被改动过（你自己改的或工具改的都算，那你知道内容）；
   - 你需要的是**当时没读到的行范围**（那就读缺失的范围，不要整个 file 重来）。

2. **一次读大段，不要连续读小段。** 需要 800 行就一次读 800 行。反复 `read_file` 1–50 行比一次读 1–800 行贵得多。

3. **不要重复列目录。** 一个目录列过一次就够了；需要定位具体文件用 `file_search` / `grep_search`，不要整个目录再列一遍。

4. **不要重复查状态。** `git status` / 错误检查 / 进程状态，在一次任务里通常只需要**改动前一次、改动后一次**。

5. **确认过的事实写下来，不要反复验证。** 查到"MySQL 里 letters 表有 33 列"就记进笔记，后面直接引用，不要再查一遍。

6. **改动与验证分开做。** 不要每改一行就跑一次全量检查；攒成一批再验证一次。

## 例外（必须重读的情况）

- **删除 / 覆盖 / 不可逆操作之前** —— 必须确认内容。这种重读是**安全成本，不是浪费**，不要为了"经济"省掉它。
- **文件可能被外部工具改过**（编辑器格式化、其他进程写入）。
- 上次读取因为过长被截断，而有明确需要补的部分。

## 自检

动手前问一句：**「这件事我今天已经知道答案了吗？」** 知道 → 直接用，别再读。
