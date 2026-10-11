Read research_context.acceptance_policy. For llm_adjudicated_v1, E-plan is an LLM
evaluation call that chooses scientific criteria, top-N values, tradeoff treatment
and missing-evidence handling before validation outcomes. E-verdict is an actual
LLM call applying that frozen plan to trusted maximum/best, every chosen top-N,
average and coverage facts. No numeric acceptance rule is supplied by the controller.
All summaries must be presented; how they support acceptance is the LLM's scientific
judgment. A tradeoff or missing metric is evidence to interpret, not an automatic
verdict. An explicit valid decision and current-trial references are required.
Keep the frozen plan unchanged after outcomes. Workflow and Meta patches cannot
rewrite this audit protocol or their own verdict. E is an evaluation role, not a
separate scientific capability or a source of measurements.

Historical mean_bootstrap and observed_pareto_v1 studies retain their originally
frozen policies for reproducibility; never silently reinterpret a saved study.
