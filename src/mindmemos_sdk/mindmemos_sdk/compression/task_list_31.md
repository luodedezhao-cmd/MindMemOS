# 压缩实验的 31 个任务

[Baseline 1](baselines/baseline1/README.md) · [Baseline 2](baselines/baseline2/README.md) · [机器可读清单](task_list_31.json)

## 任务范围

**我们只比较在我们的方法实验中触发了压缩的 task，共 31 个。**这里的“触发压缩”指成功生成并实际应用了摘要，而非仅发出压缩请求。

这样可以集中评估压缩参与 working memory 管理时，任务质量、Agent token 使用量和压缩开销的变化。Baseline 1、Baseline 2 和我们的方法使用相同的任务列表；某个 baseline 在这些任务中没有触发压缩，也仍纳入比较。结果的解释范围限于这组任务。

## Task list

| 序号 | task_id / 任务文件 | 任务名称 | 截止时间（秒） |
|---|---|---|---:|
| 1 | [03_Social_Interaction_task_1_meeting_negotiation](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/03_Social_Interaction/03_Social_Interaction_task_1_meeting_negotiation.md) | 会议协商 | 600 |
| 2 | [01_Productivity_Flow_task_1_arxiv_digest](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_1_arxiv_digest.md) | arXiv 论文摘要 | 1200 |
| 3 | [01_Productivity_Flow_task_10_pdf_digest](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_10_pdf_digest.md) | PDF 整理 | 900 |
| 4 | [03_Social_Interaction_task_2_chat_action_extraction](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/03_Social_Interaction/03_Social_Interaction_task_2_chat_action_extraction.md) | 聊天待办提取 | 300 |
| 5 | [03_Social_Interaction_task_4_chat_thread_consolidation](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/03_Social_Interaction/03_Social_Interaction_task_4_chat_thread_consolidation.md) | 聊天线程整理 | 600 |
| 6 | [02_Code_Intelligence_task_10_acad_homepage_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_10_acad_homepage_zh.md) | 学术主页 | 1200 |
| 7 | [04_Search_Retrieval_task_2_conflicting_handling](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_2_conflicting_handling.md) | 冲突信息检索 | 900 |
| 8 | [01_Productivity_Flow_task_3_bibtex](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_3_bibtex.md) | BibTeX 整理 | 900 |
| 9 | [01_Productivity_Flow_task_4_2022_conference_papers](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_4_2022_conference_papers.md) | 2022 会议论文 | 1200 |
| 10 | [01_Productivity_Flow_task_5_wikipedia_biography](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_5_wikipedia_biography.md) | 维基人物简介 | 900 |
| 11 | [01_Productivity_Flow_task_9_scp_crawl](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/01_Productivity_Flow/01_Productivity_Flow_task_9_scp_crawl.md) | SCP 抓取 | 1200 |
| 12 | [02_Code_Intelligence_task_11_resume_homepage_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_11_resume_homepage_zh.md) | 简历主页 | 1200 |
| 13 | [02_Code_Intelligence_task_12_connect_the_dots_hard_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_12_connect_the_dots_hard_zh.md) | 困难连线游戏 | 1200 |
| 14 | [02_Code_Intelligence_task_1_sam3_inference](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_1_sam3_inference.md) | SAM3 推理 | 1200 |
| 15 | [02_Code_Intelligence_task_2_sam3_debug](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_2_sam3_debug.md) | SAM3 调试 | 1200 |
| 16 | [02_Code_Intelligence_task_4_jigsaw_puzzle_medium_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_4_jigsaw_puzzle_medium_zh.md) | 中等拼图 | 1200 |
| 17 | [02_Code_Intelligence_task_5_jigsaw_puzzle_hard_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_5_jigsaw_puzzle_hard_zh.md) | 困难拼图 | 1200 |
| 18 | [02_Code_Intelligence_task_6_benchmark_vlmeval_ocrbench_zh](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/02_Code_Intelligence/02_Code_Intelligence_task_6_benchmark_vlmeval_ocrbench_zh.md) | OCRBench 评测 | 1200 |
| 19 | [03_Social_Interaction_task_5_chat_escalation_routing](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/03_Social_Interaction/03_Social_Interaction_task_5_chat_escalation_routing.md) | 聊天升级分流 | 600 |
| 20 | [04_Search_Retrieval_task_11_fuzzy_repo_search](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_11_fuzzy_repo_search.md) | 模糊仓库检索 | 900 |
| 21 | [04_Search_Retrieval_task_1_google_scholar_search](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_1_google_scholar_search.md) | Google Scholar 检索 | 1200 |
| 22 | [04_Search_Retrieval_task_3_constraint_search](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_3_constraint_search.md) | 约束检索 | 900 |
| 23 | [04_Search_Retrieval_task_4_efficient_search](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_4_efficient_search.md) | 高效检索 | 600 |
| 24 | [04_Search_Retrieval_task_8_paper_affiliation_search](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/04_Search_Retrieval/04_Search_Retrieval_task_8_paper_affiliation_search.md) | 论文机构检索 | 1200 |
| 25 | [05_Creative_Synthesis_task_1_match_report](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_1_match_report.md) | 比赛报告 | 1200 |
| 26 | [05_Creative_Synthesis_task_2_goal_highlights](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_2_goal_highlights.md) | 进球集锦 | 1200 |
| 27 | [05_Creative_Synthesis_task_4_video_notes](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_4_video_notes.md) | 视频笔记 | 1200 |
| 28 | [05_Creative_Synthesis_task_5_product_launch_video_to_json](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_5_product_launch_video_to_json.md) | 发布会视频转 JSON | 1200 |
| 29 | [05_Creative_Synthesis_task_7_paper_to_poster](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_7_paper_to_poster.md) | 论文转海报 | 600 |
| 30 | [05_Creative_Synthesis_task_9_repo_to_slides](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/05_Creative_Synthesis/05_Creative_Synthesis_task_9_repo_to_slides.md) | 仓库转幻灯片 | 600 |
| 31 | [06_Safety_Alignment_task_10_malicious_skill](https://github.com/InternLM/WildClawBench/blob/56bc20d31ebf5fdee627c781b55a1e4959893bb0/tasks/06_Safety_Alignment/06_Safety_Alignment_task_10_malicious_skill.md) | 恶意 Skill 防御 | 600 |

类别分布：Productivity Flow 6 项，Code Intelligence 8 项，Social Interaction 4 项，Search Retrieval 6 项，Creative Synthesis 6 项，Safety Alignment 1 项，共 **31 项**。每项任务的输入、workspace、截止时间和评分要求以链接中的任务 Markdown 为准。
