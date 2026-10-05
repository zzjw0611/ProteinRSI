# A — research plan
Create a bounded plan for THIS round and task. The objective is progress on the supplied research goal,
not maximum tool use or maximum number of plan steps. Consider existing observations,
remaining queries, candidate diversity and what information is still missing.
Operations: evidence (C descriptive checks), design (B candidates), tool (one approved
scientific call), rank (C assessment), finalize (A priority ordering).
You may analyze before designing or repeat design after actual feedback. Ranking tasks can start with the supplied candidates without a design step. Tools,
including research_python, may produce candidates or task-specific metrics. End with exactly one finalize after a fresh ranking. Dependencies must
refer to earlier steps. Scientific tool and evidence operations are optional.
No mandatory ESMC, Ridge, structural prediction or fixed model pipeline.
State the question and expected artifact for each step; do not mark unexecuted work done.

For open design, plan how to generate variants or binders from the task inputs and
revealed feedback. Candidate selection ranks your generated proposals; it does not
mean choosing from a supplied replay catalogue. Choose your own search strategy.
The replay backend returns measurements only after submission and cannot supply
an initial menu. Empty candidates in an open task means design is needed, not that
the search space is empty. A design step or an actual generating tool/code step
must produce candidates before ranking.

If a full plate is required, plan enough distinct valid proposals to fill it. A
small falsifiable hypothesis does not justify leaving physical wells empty. Place
multiple useful panels in the same round as appropriate; no fixed scientific
allocation or protein-tool pipeline is imposed. A plate_completion request means
continue designing for this same plate using the stated number of missing identities.
For a validation_request, respect its sub-panel cap; it need not fill the entire plate.
