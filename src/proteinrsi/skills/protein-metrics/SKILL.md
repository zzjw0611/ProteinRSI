# Protein metrics

Use the compact catalogue first. Read only the cards referenced by the chosen workflow and any explicitly selected extra cards. `cards.json` is the single source of card metadata; do not maintain a second index.

Cards identify a quantity, its provider, required inputs and result location. Read the actual ToolSpec for arguments. Obtain numerical results through registered tools and fixed extractors, not by copying or inventing numbers in an agent response. Reuse an existing matching execution to obtain several metrics. Keep candidate, structure sample, seed, atom scope, units and provenance attached.

The nine families are pLDDT, PAE, ipTM, explicitly scoped RMSD, model clash detection, ESM-family scoring, ProteinMPNN scoring, Rosetta interface energy and observed-outcome summaries. Availability is computed by the runtime; a card does not install a tool or establish that its output is available. Extra implementations and task-specific aggregation require their own version and tested input mapping.

Research screening metrics do not change the campaign's frozen evaluation policy. The existing E-plan / E-verdict path remains authoritative for method adoption.
